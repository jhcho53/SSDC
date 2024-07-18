import os
import argparse
import torch
import torch.distributed as dist
import torch.multiprocessing as mp

from torch import optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.nn.parallel import DistributedDataParallel as DDP
from torchvision.transforms import transforms
from tqdm import tqdm

from Submodules.loss.total_loss import total_loss
from Submodules.morphology import morphology_torch
from dataloader.dataLoader import KITTIDepthDataset, ToTensor
from model import DenseLiDAR

import matplotlib.pyplot as plt

parser = argparse.ArgumentParser(description='deepCompletion')
parser.add_argument('--datapath', default='datasets/', help='datapath')
parser.add_argument('--epochs', type=int, default=40, help='number of epochs to train')
parser.add_argument('--checkpoint', type=int, default=10, help='number of epochs to making checkpoint')
parser.add_argument('--batch_size', type=int, default=1, help='number of batch size to train')
parser.add_argument('--gpu_nums', type=int, default=1, help='number of gpus to train')
parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')
args = parser.parse_args()


def setup(rank, world_size):
    os.environ['MASTER_ADDR'] = '127.0.0.1'
    os.environ['MASTER_PORT'] = '29500'
    dist.init_process_group(backend='nccl', init_method='env://', world_size=world_size, rank=rank)
    torch.manual_seed(args.seed)


def cleanup():
    dist.destroy_process_group()


def save_model(model, optimizer, epoch, path):
    # Create the directory if it doesn't exist
    os.makedirs(os.path.dirname('checkpoint/'), exist_ok=True)

    torch.save({
        'epoch': epoch,
        'model_state_dict': model,
        'optimizer_state_dict': optimizer
    }, path)
    print(f'Checkpoint saved at: {path}\n')


def train(model, device, train_loader, optimizer, epoch, writer, rank):
    model.train()
    running_loss = 0.0
    running_structural_loss = 0.0
    running_depth_loss = 0.0

    for i, data in enumerate(tqdm(train_loader, desc=f"Epoch {epoch + 1} [Training]")):
        annotated_image = data['annotated_image'].to(device)
        velodyne_image = data['velodyne_image'].to(device)
        raw_image = data['raw_image'].to(device)
        targets = annotated_image.to(device)
           # 제거해야 하는 코드
        dense_pseudo_depth = model(raw_image, velodyne_image, device)
        pseudo_gt_map = morphology_torch(targets, device)

        optimizer.zero_grad()

        dense_pseudo_depth = model(raw_image, velodyne_image, device)
        dense_pseudo_depth = dense_pseudo_depth.to(device)
        dense_target = pseudo_gt_map.clone().detach().to(device)

        loss, structural_loss, depth_loss = total_loss(dense_target, targets, dense_pseudo_depth)
        loss.backward()
        optimizer.step()

        running_loss += loss.item()
        running_structural_loss += structural_loss.item()
        running_depth_loss += depth_loss.item()

    
    avg_loss = running_loss / len(train_loader)
    avg_structural_loss = running_structural_loss / len(train_loader)
    avg_depth_loss = running_depth_loss / len(train_loader)
    

    if rank == 0:
        writer.add_scalar('Loss/train', avg_loss, epoch)
        writer.add_scalar('Loss/train_structural', avg_structural_loss, epoch)
        writer.add_scalar('Loss/train_depth', avg_depth_loss, epoch)
        print(f"\nEpoch {epoch + 1} training loss: {avg_loss:.4f}")
        print(f"\nEpoch {epoch + 1} training structural loss: {avg_structural_loss:.4f}")
        print(f"\nEpoch {epoch + 1} training depth loss: {avg_depth_loss:.4f}")

    return avg_loss, avg_structural_loss, avg_depth_loss


def validate(model, device, val_loader, scheduler, epoch, writer, rank):
    model.eval()
    val_loss = 0.0
    val_structural_loss = 0.0
    val_depth_loss = 0.0

    with torch.no_grad():
        for i, data in enumerate(tqdm(val_loader, desc=f"Epoch {epoch + 1} [Validation]")):
            annotated_image = data['annotated_image'].to(device)
            velodyne_image = data['velodyne_image'].to(device)
            raw_image = data['raw_image'].to(device)
            targets = annotated_image.to(device)

            pseudo_gt_map = morphology_torch(targets, device)
            dense_pseudo_depth = model(raw_image, velodyne_image, device)
            dense_pseudo_depth = dense_pseudo_depth.to(device)
            dense_target = pseudo_gt_map.clone().detach().to(device)

            v_loss, s_loss, d_loss = total_loss(dense_target, targets, dense_pseudo_depth)

            val_loss += v_loss.item()
            val_structural_loss += s_loss.item()
            val_depth_loss += d_loss.item()

    avg_val_loss = val_loss / len(val_loader)
    avg_val_structural_loss = val_structural_loss / len(val_loader)
    avg_val_depth_loss = val_depth_loss / len(val_loader)

    if rank == 0:
        writer.add_scalar('Loss/val', avg_val_loss, epoch)
        writer.add_scalar('Loss/val_structural', avg_val_structural_loss, epoch)
        writer.add_scalar('Loss/val_depth', avg_val_depth_loss, epoch)
        print(f"\nEpoch {epoch + 1} validation loss: {avg_val_loss:.4f}")
        print(f"\nEpoch {epoch + 1} validation structural loss: {avg_val_structural_loss:.4f}")
        print(f"\nEpoch {epoch + 1} validation depth loss: {avg_val_depth_loss:.4f}")

    scheduler.step()

    return avg_val_loss, avg_val_structural_loss, avg_val_depth_loss


def main(rank, world_size):
    setup(rank, world_size)
    torch.cuda.set_device(rank)
    device = torch.device(f'cuda:{rank}')

    batch_size = args.batch_size
    writer = None
    if rank == 0:
        writer = SummaryWriter()

    root_dir = args.datapath

    train_transform = transforms.Compose([
        ToTensor()
    ])

     # Load train dataset
    try:
        train_dataset = KITTIDepthDataset(root_dir=root_dir, mode='train', transform=train_transform)
    except Exception as e:
        print(f"Error loading train dataset: {e}")
        return

    train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset, num_replicas=world_size, rank=rank)
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=False, num_workers=8, pin_memory=True,
                              sampler=train_sampler, drop_last=True)

    # Load val dataset
    try:
        val_dataset = KITTIDepthDataset(root_dir=root_dir, mode='val', transform=train_transform)
    except Exception as e:
        print(f"Error loading validation dataset: {e}")
        return
        
    val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset, num_replicas=world_size, rank=rank)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, num_workers=8, pin_memory=True,
                            sampler=val_sampler, drop_last=True)

    model = DenseLiDAR(batch_size).to(device)
    model = DDP(model, device_ids=[rank])
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=50, eta_min=1e-7, last_epoch=-1,
                                                           verbose=True)
    best_val_loss = float('inf')
    best_epoch = 0
    best_model_state = None
    best_optimizer_state = None

    num_epochs = args.epochs
    for epoch in range(num_epochs):
        train_sampler.set_epoch(epoch)
        train(model, device, train_loader, optimizer, epoch, writer, rank)
        avg_val_loss, avg_val_structural_loss, avg_val_depth_loss = validate(model, device, val_loader, scheduler,
                                                                             epoch, writer, rank)

        if epoch % args.checkpoint == 0 and rank == 0:
            save_path = f'checkpoint/epoch-{epoch}_loss-{avg_val_loss:.2f}.tar'
            save_model(model.state_dict(), optimizer.state_dict(), epoch, save_path)

        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            best_epoch = epoch + 1
            best_model_state = model.state_dict()
            best_optimizer_state = optimizer.state_dict()

    if rank == 0:
        save_model(best_model_state, best_optimizer_state, best_epoch, 'best_model.tar')
        print(f'Best model saved at epoch {best_epoch} with validation loss: {best_val_loss:.4f}')
        print('Training Finished')
        writer.close()

    cleanup()


if __name__ == "__main__":
    args.world_size = args.gpu_nums
    batch_size = int(args.batch_size / args.gpu_nums)

    world_size = args.world_size
    mp.spawn(main, args=(world_size,), nprocs=world_size, join=True)