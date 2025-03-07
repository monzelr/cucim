"""
This morphological reconstruction routine was adapted from CellProfiler, code
licensed under both GPL and BSD licenses.

Website: http://www.cellprofiler.org
Copyright (c) 2003-2009 Massachusetts Institute of Technology
Copyright (c) 2009-2011 Broad Institute
All rights reserved.
Original author: Lee Kamentsky

"""
import cupy as cp
import numpy as np
import skimage
from packaging.version import Version

from .._shared.utils import deprecate_kwarg

old_reconstruction_pyx = Version(skimage.__version__) < Version('0.20.0')


@deprecate_kwarg(kwarg_mapping={'selem': 'footprint'},
                 removed_version="23.02.00", deprecated_version="22.02.00")
def reconstruction(seed, mask, method='dilation', footprint=None, offset=None):
    """Perform a morphological reconstruction of an image.

    Morphological reconstruction by dilation is similar to basic morphological
    dilation: high-intensity values will replace nearby low-intensity values.
    The basic dilation operator, however, uses a footprint to
    determine how far a value in the input image can spread. In contrast,
    reconstruction uses two images: a "seed" image, which specifies the values
    that spread, and a "mask" image, which gives the maximum allowed value at
    each pixel. The mask image, like the footprint, limits the spread
    of high-intensity values. Reconstruction by erosion is simply the inverse:
    low-intensity values spread from the seed image and are limited by the mask
    image, which represents the minimum allowed value.

    Alternatively, you can think of reconstruction as a way to isolate the
    connected regions of an image. For dilation, reconstruction connects
    regions marked by local maxima in the seed image: neighboring pixels
    less-than-or-equal-to those seeds are connected to the seeded region.
    Local maxima with values larger than the seed image will get truncated to
    the seed value.

    Parameters
    ----------
    seed : ndarray
        The seed image (a.k.a. marker image), which specifies the values that
        are dilated or eroded.
    mask : ndarray
        The maximum (dilation) / minimum (erosion) allowed value at each pixel.
    method : {'dilation'|'erosion'}, optional
        Perform reconstruction by dilation or erosion. In dilation (or
        erosion), the seed image is dilated (or eroded) until limited by the
        mask image. For dilation, each seed value must be less than or equal
        to the corresponding mask value; for erosion, the reverse is true.
        Default is 'dilation'.
    footprint : ndarray, optional
        The neighborhood expressed as an n-D array of 1's and 0's.
        Default is the n-D square of radius equal to 1 (i.e. a 3x3 square
        for 2D images, a 3x3x3 cube for 3D images, etc.)
    offset : ndarray, optional
        The coordinates of the center of the footprint.
        Default is located on the geometrical center of the footprint, in that
        case footprint dimensions must be odd.

    Returns
    -------
    reconstructed : ndarray
       The result of morphological reconstruction.

    Examples
    --------
    >>> import cupy as cp
    >>> from cucim.skimage.morphology import reconstruction

    First, we create a sinusoidal mask image with peaks at middle and ends.

    >>> x = cp.linspace(0, 4 * np.pi)
    >>> y_mask = cp.cos(x)

    Then, we create a seed image initialized to the minimum mask value (for
    reconstruction by dilation, min-intensity values don't spread) and add
    "seeds" to the left and right peak, but at a fraction of peak value (1).

    >>> y_seed = y_mask.min() * cp.ones_like(x)
    >>> y_seed[0] = 0.5
    >>> y_seed[-1] = 0
    >>> y_rec = reconstruction(y_seed, y_mask)

    The reconstructed image (or curve, in this case) is exactly the same as the
    mask image, except that the peaks are truncated to 0.5 and 0. The middle
    peak disappears completely: Since there were no seed values in this peak
    region, its reconstructed value is truncated to the surrounding value (-1).

    As a more practical example, we try to extract the bright features of an
    image by subtracting a background image created by reconstruction.

    >>> y, x = cp.mgrid[:20:0.5, :20:0.5]
    >>> bumps = cp.sin(x) + cp.sin(y)

    To create the background image, set the mask image to the original image,
    and the seed image to the original image with an intensity offset, `h`.

    >>> h = 0.3
    >>> seed = bumps - h
    >>> background = reconstruction(seed, bumps)

    The resulting reconstructed image looks exactly like the original image,
    but with the peaks of the bumps cut off. Subtracting this reconstructed
    image from the original image leaves just the peaks of the bumps

    >>> hdome = bumps - background

    This operation is known as the h-dome of the image and leaves features
    of height `h` in the subtracted image.

    Notes
    -----
    The algorithm is taken from [1]_. Applications for grayscale reconstruction
    are discussed in [2]_ and [3]_.

    References
    ----------
    .. [1] Robinson, "Efficient morphological reconstruction: a downhill
           filter", Pattern Recognition Letters 25 (2004) 1759-1767.
    .. [2] Vincent, L., "Morphological Grayscale Reconstruction in Image
           Analysis: Applications and Efficient Algorithms", IEEE Transactions
           on Image Processing (1993)
    .. [3] Soille, P., "Morphological Image Analysis: Principles and
           Applications", Chapter 6, 2nd edition (2003), ISBN 3540429883.
    """
    from ..filters._rank_order import rank_order

    assert tuple(seed.shape) == tuple(mask.shape)
    if method == 'dilation' and cp.any(seed > mask):  # synchronize!
        raise ValueError("Intensity of seed image must be less than that "
                         "of the mask image for reconstruction by dilation.")

    elif method == 'erosion' and cp.any(seed < mask):  # synchronize!
        raise ValueError("Intensity of seed image must be greater than that "
                         "of the mask image for reconstruction by erosion.")

    try:
        from skimage.morphology._grayreconstruct import reconstruction_loop
    except ImportError:
        try:
            from skimage.morphology._greyreconstruct import reconstruction_loop
        except ImportError:
            raise ImportError("reconstruction requires scikit-image")

    if footprint is None:
        footprint = np.ones([3] * seed.ndim, dtype=bool)
    else:
        if isinstance(footprint, cp.ndarray):
            footprint = cp.asnumpy(footprint)
        footprint = footprint.astype(bool, copy=True)

    if offset is None:
        if not all([d % 2 == 1 for d in footprint.shape]):
            raise ValueError("Footprint dimensions must all be odd")
        offset = np.array([d // 2 for d in footprint.shape])
    else:
        if isinstance(offset, cp.ndarray):
            offset = cp.asnumpy(offset)
        if offset.ndim != footprint.ndim:
            raise ValueError("Offset and footprint ndims must be equal.")
        if not all([(0 <= o < d) for o, d in zip(offset, footprint.shape)]):
            raise ValueError("Offset must be included inside footprint")

    # Cross out the center of the footprint
    footprint[tuple(slice(d, d + 1) for d in offset)] = False

    # Make padding for edges of reconstructed image so we can ignore boundaries
    dims = (2, ) + \
        tuple(s1 + s2 - 1 for s1, s2 in zip(seed.shape, footprint.shape))
    inside_slices = tuple(slice(o, o + s) for o, s in zip(offset, seed.shape))
    # Set padded region to minimum image intensity and mask along first axis so
    # we can interleave image and mask pixels when sorting.
    if method == 'dilation':
        pad_value = cp.min(seed).item()
    elif method == 'erosion':
        pad_value = cp.max(seed).item()
    else:
        raise ValueError("Reconstruction method can be one of 'erosion' "
                         f"or 'dilation'. Got '{method}'.")
    # CuPy Backend: modified to allow images_dtype based on input dtype
    #               instead of float64
    images_dtype = np.promote_types(seed.dtype, mask.dtype)
    images = cp.full(dims, pad_value, dtype=images_dtype)
    images[(0, *inside_slices)] = seed
    images[(1, *inside_slices)] = mask
    isize = images.size
    if old_reconstruction_pyx:
        # scikit-image < 0.20 Cython code only supports int32_t
        signed_int_dtype = np.int32
        unsigned_int_dtype = np.uint32
    else:
        # determine whether image is large enough to require 64-bit integers
        # use -isize so we get a signed dtype rather than an unsigned one
        signed_int_dtype = np.result_type(np.min_scalar_type(-isize), np.int32)
        # the corresponding unsigned type has same char, but uppercase
        unsigned_int_dtype = np.dtype(signed_int_dtype.char.upper())

    # Create a list of strides across the array to get the neighbors within
    # a flattened array
    value_stride = np.array(images.strides[1:]) // images.dtype.itemsize
    image_stride = images.strides[0] // images.dtype.itemsize
    footprint_mgrid = np.mgrid[[slice(-o, d - o)
                                for d, o in zip(footprint.shape, offset)]]
    footprint_offsets = footprint_mgrid[:, footprint].transpose()
    nb_strides = np.array([np.sum(value_stride * footprint_offset)
                           for footprint_offset in footprint_offsets],
                          signed_int_dtype)

    # CuPy Backend: changed flatten to ravel to avoid copy
    images = images.ravel()

    # Erosion goes smallest to largest; dilation goes largest to smallest.
    index_sorted = cp.argsort(images).astype(signed_int_dtype, copy=False)
    if method == 'dilation':
        index_sorted = index_sorted[::-1]

    # Make a linked list of pixels sorted by value. -1 is the list terminator.
    index_sorted = cp.asnumpy(index_sorted)
    prev = np.full(isize, -1, signed_int_dtype)
    next = np.full(isize, -1, signed_int_dtype)
    prev[index_sorted[1:]] = index_sorted[:-1]
    next[index_sorted[:-1]] = index_sorted[1:]

    # Cython inner-loop compares the rank of pixel values.
    if method == 'dilation':
        value_rank, value_map = rank_order(images)
    elif method == 'erosion':
        value_rank, value_map = rank_order(-images)
        value_map = -value_map

    # TODO: implement reconstruction_loop on the GPU? For now, run it on host.
    start = index_sorted[0]
    value_rank = cp.asnumpy(value_rank.astype(unsigned_int_dtype, copy=False))
    reconstruction_loop(value_rank, prev, next, nb_strides, start,
                        image_stride)

    # Reshape reconstructed image to original image shape and remove padding.
    value_rank = cp.asarray(value_rank[:image_stride])

    rec_img = value_map[value_rank]
    rec_img.shape = dims[1:]
    return rec_img[inside_slices]
