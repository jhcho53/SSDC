import torch
import torch.nn.functional as F
from typing import List, Optional

#cv2.mideanBluer => torch
def median_blur_torch(image_tensor, kernel_size):
    pad_size = kernel_size // 2
    padded_image = F.pad(image_tensor, (pad_size, pad_size, pad_size, pad_size), mode='reflect')

    
    N, C, H, W = padded_image.shape
    unfolded = F.unfold(padded_image, kernel_size=(kernel_size, kernel_size))
    unfolded = unfolded.view(N, C, kernel_size * kernel_size, -1)
    
    median_values, _ = unfolded.median(dim=2)
    
    median_image = median_values.view(N, C, H - 2 * pad_size, W - 2 * pad_size)
    return median_image


def gaussian(kernel_size, sigma):
    """
    Compute a Gaussian kernel.

    Args:
        kernel_size (int): The size of the Gaussian kernel.
        sigma (float): The standard deviation of the Gaussian kernel.

    Returns:
        torch.Tensor: The Gaussian kernel.
    """
    x = torch.arange(-(kernel_size // 2), kernel_size // 2 + 1).float()
    y = x.unsqueeze(0)
    kernel = torch.exp(-(x ** 2 + y ** 2) / (2 * sigma ** 2))
    return kernel / kernel.sum()


def bilateral_filter(input, kernel_size, sigma_spatial, sigma_color):
    input = (input - input.mean()) / input.std()
    """
    Apply a bilateral filter to the input tensor.

    Args:
        input (torch.Tensor): The input tensor. Shape: (batch_size, 1, height, width)
        kernel_size (int): The size of the Gaussian kernel.
        sigma_spatial (float): The standard deviation of the spatial Gaussian kernel.
        sigma_color (float): The standard deviation of the color Gaussian kernel.

    Returns:
        torch.Tensor: The filtered output tensor.
    """
    # Create spatial Gaussian filter
    spatial_gaussian = gaussian(kernel_size, sigma_spatial).unsqueeze(0).unsqueeze(0).to(input.device)
    #print(spatial_gaussian)
    # Pad the input
    padding = kernel_size // 2
    input_padded = F.pad(input, (padding, padding, padding, padding), mode='reflect')

    # Compute the color distance

    color_distance = input_padded[:, 0] - input_padded[:, 0].mean(dim=[1], keepdim=True)
    print(color_distance)

    color_gaussian = torch.exp(-(color_distance ** 2) / (2 * sigma_color ** 2))
    print(torch.exp(torch.tensor(-1.5**2/8)))
    print(color_gaussian)

    print("input_padded")
    print(input_padded.shape)
    print("color_gaussian")
    print(color_gaussian.unsqueeze(1).shape)
    # Apply the spatial Gaussian filter
    filtered = F.conv2d(input_padded * color_gaussian.unsqueeze(1), spatial_gaussian, padding=padding, stride=1)
    normalization = F.conv2d(color_gaussian.unsqueeze(1), spatial_gaussian, padding=padding, stride=1)

    print(filtered[:, 0].shape)
    print(filtered[:, 0])
    print(normalization[:, 0].shape)
    print(normalization[:, 0])

    return filtered[:, 0] / normalization[:, 0]




def _neight2channels_like_kernel(kernel: torch.Tensor) -> torch.Tensor:
    h, w = kernel.size()
    kernel = torch.eye(h * w, dtype=kernel.dtype, device=kernel.device)
    return kernel.view(h * w, 1, h, w)

def dilation(
    tensor: torch.Tensor,
    kernel: torch.Tensor,
    structuring_element: Optional[torch.Tensor] = None,
    origin: Optional[List[int]] = None,
    border_type: str = "geodesic",
    border_value: float = 0.0,
    max_val: float = 1e4,
) -> torch.Tensor:


    if not isinstance(tensor, torch.Tensor):
        raise TypeError(f"Input type is not a torch.Tensor. Got {type(tensor)}")

    if len(tensor.shape) != 4:
        raise ValueError(f"Input size must have 4 dimensions. Got {tensor.dim()}")

    if not isinstance(kernel, torch.Tensor):
        raise TypeError(f"Kernel type is not a torch.Tensor. Got {type(kernel)}")

    if len(kernel.shape) != 2:
        raise ValueError(f"Kernel size must have 2 dimensions. Got {kernel.dim()}")

    # origin
    se_h, se_w = kernel.shape
    if origin is None:
        origin = [se_h // 2, se_w // 2]

    # padding
    pad_e: List[int] = [origin[1], se_w - origin[1] - 1, origin[0], se_h - origin[0] - 1]
    if border_type == "geodesic":
        border_value = -max_val
        border_type = "constant"
    output: torch.Tensor = F.pad(tensor, pad_e, mode=border_type, value=border_value)

    # computation
    neighborhood = torch.zeros_like(kernel)
    neighborhood[kernel == 0] = -max_val

    B, C, H, W = tensor.size()
    h_pad, w_pad = output.shape[-2:]
    reshape_kernel = _neight2channels_like_kernel(kernel)

    output, _ = F.conv2d(
        output.view(B * C, 1, h_pad, w_pad), reshape_kernel, padding=0, bias=neighborhood.view(-1).flip(0)
    ).max(dim=1)
    output = output.view(B, C, H, W)

    return output.view_as(tensor)

def erosion(
    tensor: torch.Tensor,
    kernel: torch.Tensor,
    structuring_element: Optional[torch.Tensor] = None,
    origin: Optional[List[int]] = None,
    border_type: str = "geodesic",
    border_value: float = 0.0,
    max_val: float = 1e4,
    engine: str = "unfold",
) -> torch.Tensor:
    r"""Return the eroded image applying the same kernel in each channel.

    .. image:: _static/img/erosion.png

    The kernel must have 2 dimensions.

    Args:
        tensor: Image with shape :math:`(B, C, H, W)`.
        kernel: Positions of non-infinite elements of a flat structuring element. Non-zero values give
            the set of neighbors of the center over which the operation is applied. Its shape is :math:`(k_x, k_y)`.
            For full structural elements use torch.ones_like(structural_element).
        structuring_element (torch.Tensor, optional): Structuring element used for the grayscale dilation.
            It may be a non-flat structuring element.
        origin: Origin of the structuring element. Default: ``None`` and uses the center of
            the structuring element as origin (rounding towards zero).
        border_type: It determines how the image borders are handled, where ``border_value`` is the value
            when ``border_type`` is equal to ``constant``. Default: ``geodesic`` which ignores the values that are
            outside the image when applying the operation.
        border_value: Value to fill past edges of input if border_type is ``constant``.
        max_val: The value of the infinite elements in the kernel.
        engine: ``convolution`` is faster and less memory hungry, and ``unfold`` is more stable numerically

    Returns:
        Eroded image with shape :math:`(B, C, H, W)`.

    .. note::
       See a working example `here <https://kornia.github.io/tutorials/nbs/morphology_101.html>`__.

    Example:
        >>> tensor = torch.rand(1, 3, 5, 5)
        >>> kernel = torch.ones(5, 5)
        >>> output = erosion(tensor, kernel)
    """

    if not isinstance(tensor, torch.Tensor):
        raise TypeError(f"Input type is not a torch.Tensor. Got {type(tensor)}")

    if len(tensor.shape) != 4:
        raise ValueError(f"Input size must have 4 dimensions. Got {tensor.dim()}")

    if not isinstance(kernel, torch.Tensor):
        raise TypeError(f"Kernel type is not a torch.Tensor. Got {type(kernel)}")

    if len(kernel.shape) != 2:
        raise ValueError(f"Kernel size must have 2 dimensions. Got {kernel.dim()}")

    # origin
    se_h, se_w = kernel.shape
    if origin is None:
        origin = [se_h // 2, se_w // 2]

    # pad
    pad_e: List[int] = [origin[1], se_w - origin[1] - 1, origin[0], se_h - origin[0] - 1]
    if border_type == "geodesic":
        border_value = max_val
        border_type = "constant"
    output: torch.Tensor = F.pad(tensor, pad_e, mode=border_type, value=border_value)

    # computation
    if structuring_element is None:
        neighborhood = torch.zeros_like(kernel)
        neighborhood[kernel == 0] = -max_val
    else:
        neighborhood = structuring_element.clone()
        neighborhood[kernel == 0] = -max_val

    if engine == "unfold":
        output = output.unfold(2, se_h, 1).unfold(3, se_w, 1)
        output, _ = torch.min(output - neighborhood, 4)
        output, _ = torch.min(output, 4)
    elif engine == "convolution":
        B, C, H, W = tensor.size()
        Hpad, Wpad = output.shape[-2:]
        reshape_kernel = _neight2channels_like_kernel(kernel)
        output, _ = F.conv2d(
            output.view(B * C, 1, Hpad, Wpad), reshape_kernel, padding=0, bias=-neighborhood.view(-1)
        ).min(dim=1)
        output = output.view(B, C, H, W)
    else:
        raise NotImplementedError(f"engine {engine} is unknown, use 'convolution' or 'unfold'")

    return output
