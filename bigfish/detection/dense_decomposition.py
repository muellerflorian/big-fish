# -*- coding: utf-8 -*-
# Author: Arthur Imbert <arthur.imbert.pro@gmail.com>
# License: BSD 3 clause

"""
Functions to detect dense and bright regions (with potential clustered spots),
then use gaussian functions to correct a misdetection in these regions.
"""

import warnings

import numpy as np

import bigfish.stack as stack
from .spot_modeling import build_reference_spot, modelize_spot, precompute_erf
from .spot_modeling import _gaussian_2d, _initialize_grid_2d
from .spot_modeling import _gaussian_3d, _initialize_grid_3d

from skimage.measure import regionprops
from skimage.measure import label


# ### Main function ###

def decompose_dense(image, spots, voxel_size_z=None, voxel_size_yx=100,
                    psf_z=None, psf_yx=200, alpha=0.5, beta=1, gamma=5):
    """Detect dense and bright regions with potential clustered spots and
    simulate a more realistic number of spots in these regions.

    1) We estimate image background with a large gaussian filter. We then
    remove the background from the original image to denoise it.
    2) We build a reference spot by aggregating predetected spots.
    3) We fit gaussian parameters on the reference spots.
    4) We detect dense regions to decompose.
    5) We simulate as many gaussians as possible in the candidate regions.

    Parameters
    ----------
    image : np.ndarray
        Image with shape (z, y, x) or (y, x).
    spots : np.ndarray, np.int64
        Coordinate of the spots with shape (nb_spots, 3) or (nb_spots, 2)
        for 3-d or 2-d images respectively.
    voxel_size_z : int or float or None
        Height of a voxel, along the z axis, in nanometer. If None, image is
        considered in 2-d.
    voxel_size_yx : int or float
        Size of a voxel on the yx plan, in nanometer.
    psf_z : int or float or None
        Theoretical size of the PSF emitted by a spot in the z plan,
        in nanometer. If None, image is considered in 2-d.
    psf_yx : int or float
        Theoretical size of the PSF emitted by a spot in the yx plan,
        in nanometer.
    alpha : int or float
        Intensity percentile used to compute the reference spot, between 0
        and 1. The higher, the brighter are the spots simulated in the dense
        regions. Consequently, a high intensity score reduces the number of
        spots added. Default is 0.5, meaning the reference spot considered is
        the median spot.
    beta : int or float
        Multiplicative factor for the intensity threshold of a dense region.
        Default is 1. Threshold is computed with the formula :
            threshold = beta * max(median_spot)
    gamma : int or float
        Multiplicative factor use to compute a gaussian scale :
            large_sigma = gamma * psf / voxel_size
        We perform a large gaussian filter with such scale to estimate image
        background and remove it from original image. A large gamma increases
        the scale of the gaussian filter and smooth the estimated background.
        To decompose very large bright areas, a larger gamma should be set.
        If 0, image is not denoised.

    Returns
    -------
    spots : np.ndarray, np.int64
        Coordinate of the spots detected, with shape (nb_spots, 3) or
        (nb_spots, 2). One coordinate per dimension (zyx or yx coordinates).
    dense_regions : np.ndarray, np.int64
        Array with shape (nb_regions, 7) or (nb_regions, 6). One coordinate
        per dimension for the region centroid (zyx or yx coordinates), the
        number of RNAs detected in the region, the area of the region, its
        average intensity value and its index.
    reference_spot : np.ndarray
        Reference spot in 3-d or 2-d.

    """
    # check parameters
    stack.check_array(image,
                      ndim=[2, 3],
                      dtype=[np.uint8, np.uint16, np.float32, np.float64])
    stack.check_array(spots, ndim=2, dtype=np.int64)
    stack.check_parameter(voxel_size_z=(int, float, type(None)),
                          voxel_size_yx=(int, float),
                          psf_z=(int, float, type(None)),
                          psf_yx=(int, float),
                          alpha=(int, float),
                          beta=(int, float),
                          gamma=(int, float))
    if alpha < 0 or alpha > 1:
        raise ValueError("'alpha' should be a value between 0 and 1, not {0}"
                         .format(alpha))
    if beta < 0:
        raise ValueError("'beta' should be a positive value, not {0}"
                         .format(beta))
    if gamma < 0:
        raise ValueError("'gamma' should be a positive value, not {0}"
                         .format(gamma))

    # check number of dimensions
    ndim = image.ndim
    if ndim == 3 and voxel_size_z is None:
        raise ValueError("Provided image has {0} dimensions but "
                         "'voxel_size_z' parameter is missing.".format(ndim))
    if ndim == 3 and psf_z is None:
        raise ValueError("Provided image has {0} dimensions but "
                         "'psf_z' parameter is missing.".format(ndim))
    if ndim != spots.shape[1]:
        raise ValueError("Provided image has {0} dimensions but spots are "
                         "detected in {1} dimensions."
                         .format(ndim, spots.shape[1]))
    if ndim == 2:
        voxel_size_z, psf_z = None, None

    # case where no spot were detected
    if spots.size == 0:
        dense_regions = np.array([], dtype=np.int64).reshape((0, ndim + 4))
        reference_spot = np.zeros((5,) * ndim, dtype=image.dtype)
        return spots, dense_regions, reference_spot

    # compute expected standard deviation of the spots
    sigma = stack.get_sigma(voxel_size_z, voxel_size_yx, psf_z, psf_yx)
    large_sigma = tuple([sigma_ * gamma for sigma_ in sigma])

    # denoise the image
    if gamma > 0:
        image_denoised = stack.remove_background_gaussian(
            image,
            sigma=large_sigma)
    else:
        image_denoised = image.copy()

    # build a reference median spot
    reference_spot = build_reference_spot(
        image_denoised,
        spots,
        voxel_size_z, voxel_size_yx, psf_z, psf_yx,
        alpha)

    # case with an empty frame as reference spot
    if reference_spot.sum() == 0:
        dense_regions = np.array([], dtype=np.int64).reshape((0, ndim + 4))
        return spots, dense_regions, reference_spot

    # fit a gaussian function on the reference spot to be able to simulate it
    parameters_fitted = modelize_spot(
        reference_spot, voxel_size_z, voxel_size_yx, psf_z, psf_yx)
    if ndim == 3:
        sigma_z, sigma_yx, amplitude, background = parameters_fitted
    else:
        sigma_z = None
        sigma_yx, amplitude, background = parameters_fitted

    # use connected components to detect dense and bright regions
    regions_to_decompose, spots_out_regions, region_size = get_dense_region(
        image_denoised,
        spots,
        voxel_size_z, voxel_size_yx, psf_z, psf_yx,
        beta)

    # case where no region where detected
    if regions_to_decompose.size == 0:
        dense_regions = np.array([], dtype=np.int64).reshape((0, ndim + 4))
        return spots, dense_regions, reference_spot

    # precompute gaussian function values
    max_grid = max(200, region_size + 1)
    precomputed_gaussian = precompute_erf(
        voxel_size_z, voxel_size_yx, sigma_z, sigma_yx, max_grid=max_grid)

    # simulate gaussian mixtures in the dense regions
    spots_in_regions, dense_regions = simulate_gaussian_mixture(
        image=image_denoised,
        candidate_regions=regions_to_decompose,
        voxel_size_z=voxel_size_z,
        voxel_size_yx=voxel_size_yx,
        sigma_z=sigma_z,
        sigma_yx=sigma_yx,
        amplitude=amplitude,
        background=background,
        precomputed_gaussian=precomputed_gaussian)

    # normally the number of detected spots should increase
    if len(spots_out_regions) + len(spots_in_regions) < len(spots):
        warnings.warn("Problem occurs during the decomposition of dense "
                      "regions. Less spots are detected after the "
                      "decomposition than before.",
                      UserWarning)

    # merge outside and inside spots
    spots = np.concatenate((spots_out_regions, spots_in_regions[:, :ndim]),
                           axis=0)

    return spots, dense_regions, reference_spot


# ### Dense regions ###

def get_dense_region(image, spots, voxel_size_z=None, voxel_size_yx=100,
                     psf_z=None, psf_yx=200, beta=1):
    """Detect and filter dense and bright regions.

    A candidate region follows has at least 2 connected pixels above a
    specific threshold.

    Parameters
    ----------
    image : np.ndarray
        Image with shape (z, y, x) or (y, x).
    spots : np.ndarray, np.int64
        Coordinate of the spots with shape (nb_spots, 3) or (nb_spots, 2).
    voxel_size_z : int or float or None
        Height of a voxel, along the z axis, in nanometer. If None, we
        consider a 2-d image.
    voxel_size_yx : int or float
        Size of a voxel on the yx plan, in nanometer.
    psf_z : int or float or None
        Theoretical size of the PSF emitted by a spot in the z plan,
        in nanometer. If None, we consider a 2-d image.
    psf_yx : int or float
        Theoretical size of the PSF emitted by a spot in the yx plan,
        in nanometer.
    beta : int or float
        Multiplicative factor for the intensity threshold of a dense region.
        Default is 1. Threshold is computed with the formula :
            threshold = beta * max(median_spot)

    Returns
    -------
    dense_regions : np.ndarray
        Array with filtered skimage.measure._regionprops._RegionProperties.
    spots_out_region : np.ndarray, np.int64
        Coordinate of the spots detected out of dense regions, with shape
        (nb_spots, 3) or (nb_spots, 2). One coordinate per dimension (zyx or
        yx coordinates).
    max_size : int
        Maximum size of the regions.

    """
    # check parameters
    stack.check_array(image,
                      ndim=[2, 3],
                      dtype=[np.uint8, np.uint16, np.float32, np.float64])
    stack.check_array(spots, ndim=2, dtype=np.int64)
    stack.check_parameter(voxel_size_z=(int, float, type(None)),
                          voxel_size_yx=(int, float),
                          psf_z=(int, float, type(None)),
                          psf_yx=(int, float),
                          beta=(int, float))
    if beta < 0:
        raise ValueError("'beta' should be a positive value, not {0}"
                         .format(beta))

    # check number of dimensions
    ndim = image.ndim
    if ndim == 3 and voxel_size_z is None:
        raise ValueError("Provided image has {0} dimensions but "
                         "'voxel_size_z' parameter is missing.".format(ndim))
    if ndim == 3 and psf_z is None:
        raise ValueError("Provided image has {0} dimensions but "
                         "'psf_z' parameter is missing.".format(ndim))
    if ndim != spots.shape[1]:
        raise ValueError("Provided image has {0} dimensions but spots are "
                         "detected in {1} dimensions."
                         .format(ndim, spots.shape[1]))
    if ndim == 2:
        voxel_size_z, psf_z = None, None

    # estimate median spot value and a threshold to detect dense regions
    median_spot = build_reference_spot(
        image,
        spots,
        voxel_size_z, voxel_size_yx, psf_z, psf_yx,
        alpha=0.5)
    threshold = int(median_spot.max() * beta)

    # get connected regions
    connected_regions = _get_connected_region(image, threshold)

    # filter connected regions
    (dense_regions, spots_out_region, max_size) = _filter_connected_region(
        image, connected_regions, spots)

    return dense_regions, spots_out_region, max_size


def _get_connected_region(image, threshold):
    """Find connected regions above a fixed threshold.

    Parameters
    ----------
    image : np.ndarray
        Image with shape (z, y, x) or (y, x).
    threshold : int or float
        A threshold to detect peaks.

    Returns
    -------
    cc : np.ndarray, np.int64
        Image labelled with shape (z, y, x) or (y, x).

    """
    # compute binary mask of the filtered image
    mask = image > threshold

    # find connected components
    cc = label(mask)

    return cc


def _filter_connected_region(image, connected_component, spots):
    """Filter dense and bright regions (defined as connected component
    regions).

    A candidate region has at least 2 connected pixels above a specific
    threshold.

    Parameters
    ----------
    image : np.ndarray
        Image with shape (z, y, x) or (y, x).
    connected_component : np.ndarray, np.int64
        Image labelled with shape (z, y, x) or (y, x).
    spots : np.ndarray, np.int64
        Coordinate of the spots with shape (nb_spots, 3) or (nb_spots, 2).

    Returns
    -------
    regions_filtered : np.ndarray
        Array with filtered skimage.measure._regionprops._RegionProperties.
    spots_out_region : np.ndarray, np.int64
        Coordinate of the spots outside the regions with shape (nb_spots, 3)
        or (nb_spots, 2).
    max_region_size : int
        Maximum size of the regions.

    """
    # get properties of the different connected regions
    regions = regionprops(connected_component, intensity_image=image)

    # get different features of the regions
    area = []
    bbox = []
    for i, region in enumerate(regions):
        area.append(region.area)
        bbox.append(region.bbox)
    regions = np.array(regions)
    area = np.array(area)
    bbox = np.array(bbox)

    # keep regions with a minimum size
    big_area = area >= 2
    regions_filtered = regions[big_area]
    bbox_filtered = bbox[big_area]

    # case where no region big enough were detected
    if regions.size == 0:
        regions_filtered = np.array([])
        return regions_filtered, spots, 0

    spots_out_region, max_region_size = _filter_spot_out_candidate_regions(
        bbox_filtered, spots, nb_dim=image.ndim)

    return regions_filtered, spots_out_region, max_region_size


def _filter_spot_out_candidate_regions(candidate_bbox, spots, nb_dim):
    """Filter spots out of the dense regions.

    Parameters
    ----------
    candidate_bbox : List[Tuple]
        List of Tuples with the bounding box coordinates.
    spots : np.ndarray, np.int64
        Coordinate of the spots with shape (nb_spots, 3) or (nb_spots, 2).
    nb_dim : int
        Number of dimensions to consider (2 or 3).

    Returns
    -------
    spots_out_region : np.ndarray, np.int64
        Coordinate of the spots outside the regions with shape (nb_spots, 3)
        or (nb_spots, 2).
    max_region_size : int
        Maximum size of the regions.

    """
    # initialization
    mask_spots_out = np.ones(spots[:, 0].shape, dtype=bool)
    max_region_size = 0

    # get detected spots outside 3-d regions
    if nb_dim == 3:
        for box in candidate_bbox:
            (min_z, min_y, min_x, max_z, max_y, max_x) = box

            # get the size of the biggest region
            size_z = max_z - min_z
            size_y = max_y - min_y
            size_x = max_x - min_x
            max_region_size = max(max_region_size, size_z, size_y, size_x)

            # get coordinates of spots inside the region
            mask_spots_in = spots[:, 0] < max_z
            mask_spots_in = (mask_spots_in & (spots[:, 1] < max_y))
            mask_spots_in = (mask_spots_in & (spots[:, 2] < max_x))
            mask_spots_in = (mask_spots_in & (min_z <= spots[:, 0]))
            mask_spots_in = (mask_spots_in & (min_y <= spots[:, 1]))
            mask_spots_in = (mask_spots_in & (min_x <= spots[:, 2]))
            mask_spots_out = mask_spots_out & (~mask_spots_in)

    # get detected spots outside 2-d regions
    else:
        for box in candidate_bbox:
            (min_y, min_x, max_y, max_x) = box

            # get the size of the biggest region
            size_y = max_y - min_y
            size_x = max_x - min_x
            max_region_size = max(max_region_size, size_y, size_x)

            # get coordinates of spots inside the region
            mask_spots_in = spots[:, 0] < max_y
            mask_spots_in = (mask_spots_in & (spots[:, 1] < max_x))
            mask_spots_in = (mask_spots_in & (min_y <= spots[:, 0]))
            mask_spots_in = (mask_spots_in & (min_x <= spots[:, 1]))
            mask_spots_out = mask_spots_out & (~mask_spots_in)

    # keep apart spots inside a region
    spots_out_region = spots.copy()
    spots_out_region = spots_out_region[mask_spots_out]

    return spots_out_region, int(max_region_size)


# ### Gaussian simulation ###

def simulate_gaussian_mixture(image, candidate_regions, voxel_size_z=None,
                              voxel_size_yx=100, sigma_z=None, sigma_yx=200,
                              amplitude=100, background=0,
                              precomputed_gaussian=None):
    """Simulate as many gaussians as possible in the candidate dense
    regions in order to get a more realistic number of spots.

    Parameters
    ----------
    image : np.ndarray
        Image with shape (z, y, x) or (y, x).
    candidate_regions : np.ndarray
        Array with filtered skimage.measure._regionprops._RegionProperties.
    voxel_size_z : int or float or None
        Height of a voxel, along the z axis, in nanometer. If None, we
        consider a 2-d image.
    voxel_size_yx : int or float
        Size of a voxel on the yx plan, in nanometer.
    sigma_z : int or float or None
        Standard deviation of the gaussian along the z axis, in nanometer.
        If None, we consider a 2-d image.
    sigma_yx : int or float
        Standard deviation of the gaussian along the yx axis, in nanometer.
    amplitude : float
        Amplitude of the gaussian.
    background : float
        Background minimum value of the image.
    precomputed_gaussian : Tuple[np.ndarray]
        Tuple with one tables of precomputed values for the erf, with shape
        (nb_value, 2). One table per dimension.

    Returns
    -------
    spots_in_regions : np.ndarray, np.int64
        Coordinate of the spots detected inside dense regions, with shape
        (nb_spots, 4) or (nb_spots, 3). One coordinate per dimension (zyx
        or yx coordinates) plus the index of the region.
    regions : np.ndarray, np.int64
        Array with shape (nb_regions, 7) or (nb_regions, 6). One coordinate
        per dimension for the region centroid (zyx or yx coordinates), the
        number of RNAs detected in the region, the area of the region, its
        average intensity value and its index.

    """
    # check parameters
    stack.check_array(image,
                      ndim=[2, 3],
                      dtype=[np.uint8, np.uint16, np.float32, np.float64])
    stack.check_parameter(candidate_regions=np.ndarray,
                          voxel_size_z=(int, float, type(None)),
                          voxel_size_yx=(int, float),
                          sigma_z=(int, float, type(None)),
                          sigma_yx=(int, float),
                          amplitude=float,
                          background=float)
    if background < 0:
        raise ValueError("Background value can't be negative: {0}"
                         .format(background))

    # check number of dimensions
    ndim = image.ndim
    if ndim == 3 and voxel_size_z is None:
        raise ValueError("Provided image has {0} dimensions but "
                         "'voxel_size_z' parameter is missing."
                         .format(ndim))
    if ndim == 3 and sigma_z is None:
        raise ValueError("Provided image has {0} dimensions but "
                         "'sigma_z' parameter is missing.".format(ndim))
    if ndim == 2:
        voxel_size_z, sigma_z = None, None

    # simulate gaussian mixtures in the candidate regions...
    spots_in_regions = []
    regions = []

    # ... for 3-d regions...
    if image.ndim == 3:

        for i_region, region in enumerate(candidate_regions):
            image_region, _, coord_gaussian = _gaussian_mixture_3d(
                image,
                region,
                voxel_size_z,
                voxel_size_yx,
                sigma_z,
                sigma_yx,
                amplitude,
                background,
                precomputed_gaussian)

            # get coordinates of spots and regions in the original image
            box = region.bbox
            (min_z, min_y, min_x, _, _, _) = box
            coord = np.array(coord_gaussian, dtype=np.float64)
            coord[:, 0] = (coord[:, 0] / voxel_size_z) + min_z
            coord[:, 1] = (coord[:, 1] / voxel_size_yx) + min_y
            coord[:, 2] = (coord[:, 2] / voxel_size_yx) + min_x
            spots_in_region = np.zeros((coord.shape[0], 4), dtype=np.int64)
            spots_in_region[:, :3] = coord
            spots_in_region[:, 3] = i_region
            spots_in_regions.append(spots_in_region)
            region_z, region_y, region_x = tuple(coord[0])
            nb_rna_region = coord.shape[0]
            region_area = region.area
            region_intensity = region.mean_intensity
            regions.append([region_z, region_y, region_x, nb_rna_region,
                            region_area, region_intensity, i_region])

    # ... or 2-d regions
    else:

        for i_region, region in enumerate(candidate_regions):
            image_region, _, coord_gaussian = _gaussian_mixture_2d(
                image,
                region,
                voxel_size_yx,
                sigma_yx,
                amplitude,
                background,
                precomputed_gaussian)

            # get coordinates of spots and regions in the original image
            box = region.bbox
            (min_y, min_x, _, _) = box
            coord = np.array(coord_gaussian, dtype=np.float64)
            coord[:, 0] = (coord[:, 0] / voxel_size_yx) + min_y
            coord[:, 1] = (coord[:, 1] / voxel_size_yx) + min_x
            spots_in_region = np.zeros((coord.shape[0], 3), dtype=np.int64)
            spots_in_region[:, :2] = coord
            spots_in_region[:, 2] = i_region
            spots_in_regions.append(spots_in_region)
            region_y, region_x = tuple(coord[0])
            nb_rna_region = coord.shape[0]
            region_area = region.area
            region_intensity = region.mean_intensity
            regions.append([region_y, region_x, nb_rna_region,
                            region_area, region_intensity, i_region])

    spots_in_regions = np.concatenate(spots_in_regions, axis=0)
    regions = np.array(regions, dtype=np.int64)

    return spots_in_regions, regions


def _gaussian_mixture_3d(image, region, voxel_size_z, voxel_size_yx, sigma_z,
                         sigma_yx, amplitude, background, precomputed_gaussian,
                         limit_gaussian=1000):
    """Fit as many 3-d gaussians as possible in a candidate region.

    Parameters
    ----------
    image : np.ndarray, np.uint
        A 3-d image with detected spot and shape (z, y, x).
    region : skimage.measure._regionprops._RegionProperties
        Properties of a candidate region.
    voxel_size_z : int or float
        Height of a voxel, along the z axis, in nanometer.
    voxel_size_yx : int or float
        Size of a voxel on the yx plan, in nanometer.
    sigma_z : int or float
        Standard deviation of the gaussian along the z axis, in pixel.
    sigma_yx : int or float
        Standard deviation of the gaussian along the yx axis, in pixel.
    amplitude : float
        Amplitude of the gaussian.
    background : float
        Background minimum value of the image.
    precomputed_gaussian : Tuple[np.ndarray]
        Tuple with one tables of precomputed values for the erf, with shape
        (nb_value, 2). One table per dimension.
    limit_gaussian : int
        Limit number of gaussian to fit into this region.

    Returns
    -------
    image_region : np.ndarray, np.uint
        A 3-d image with detected spots and shape (z, y, x).
    best_simulation : np.ndarray, np.uint
        A 3-d image with simulated spots and shape (z, y, x).
    positions_gaussian : List[List]
        List of positions (as a list [z, y, x]) for the different gaussian
        simulations used in the mixture.

    """
    # get an image of the region
    box = tuple(region.bbox)
    image_region = image[box[0]:box[3], box[1]:box[4], box[2]:box[5]]
    image_region_raw = np.reshape(image_region, image_region.size)
    image_region_raw = image_region_raw.astype(np.float64)

    # build a grid to represent this image
    grid = _initialize_grid_3d(image_region, voxel_size_z, voxel_size_yx)

    # add a gaussian for each local maximum while the RSS decreases
    simulation = np.zeros_like(image_region_raw)
    residual = image_region_raw - simulation
    ssr = np.sum(residual ** 2)
    diff_ssr = -1
    nb_gaussian = 0
    best_simulation = simulation.copy()
    positions_gaussian = []
    while diff_ssr < 0 or nb_gaussian == limit_gaussian:
        position_gaussian = np.argmax(residual)
        positions_gaussian.append(list(grid[:, position_gaussian]))
        simulation += _gaussian_3d(grid=grid,
                                   mu_z=float(positions_gaussian[-1][0]),
                                   mu_y=float(positions_gaussian[-1][1]),
                                   mu_x=float(positions_gaussian[-1][2]),
                                   sigma_z=sigma_z,
                                   sigma_yx=sigma_yx,
                                   voxel_size_z=voxel_size_z,
                                   voxel_size_yx=voxel_size_yx,
                                   psf_amplitude=amplitude,
                                   psf_background=background,
                                   precomputed=precomputed_gaussian)
        residual = image_region_raw - simulation
        new_ssr = np.sum(residual ** 2)
        diff_ssr = new_ssr - ssr
        ssr = new_ssr
        nb_gaussian += 1
        background = 0

        if diff_ssr < 0:
            best_simulation = simulation.copy()

    if 1 < nb_gaussian < limit_gaussian:
        positions_gaussian.pop(-1)
    elif nb_gaussian == limit_gaussian:
        warnings.warn("Problem occurs during the decomposition of a dense "
                      "region. More than {0} spots seem to be necessary to "
                      "reproduce the candidate region and decomposition was "
                      "stopped early. Set a higher limit or check a potential "
                      "artifact in the image if you do not expect such a "
                      "large region to be decomposed.".format(limit_gaussian),
                      UserWarning)

    best_simulation = np.reshape(best_simulation, image_region.shape)
    max_value_dtype = np.iinfo(image_region.dtype).max
    best_simulation = np.clip(best_simulation, 0, max_value_dtype)
    best_simulation = best_simulation.astype(image_region.dtype)

    return image_region, best_simulation, positions_gaussian


def _gaussian_mixture_2d(image, region, voxel_size_yx, sigma_yx, amplitude,
                         background, precomputed_gaussian,
                         limit_gaussian=1000):
    """Fit as many 2-d gaussians as possible in a candidate region.

    Parameters
    ----------
    image : np.ndarray, np.uint
        A 2-d image with detected spot and shape (y, x).
    region : skimage.measure._regionprops._RegionProperties
        Properties of a candidate region.
    voxel_size_yx : int or float
        Size of a voxel on the yx plan, in nanometer.
    sigma_yx : int or float
        Standard deviation of the gaussian along the yx axis, in pixel.
    amplitude : float
        Amplitude of the gaussian.
    background : float
        Background minimum value of the image.
    precomputed_gaussian : Tuple[np.ndarray]
        Tuple with one tables of precomputed values for the erf, with shape
        (nb_value, 2). One table per dimension.
    limit_gaussian : int
        Limit number of gaussian to fit into this region.

    Returns
    -------
    image_region : np.ndarray, np.uint
        A 2-d image with detected spots and shape (y, x).
    best_simulation : np.ndarray, np.uint
        A 2-d image with simulated spots and shape (y, x).
    positions_gaussian : List[List]
        List of positions (as a list [y, x]) for the different gaussian
        simulations used in the mixture.

    """
    # get an image of the region
    box = tuple(region.bbox)
    image_region = image[box[0]:box[2], box[1]:box[3]]
    image_region_raw = np.reshape(image_region, image_region.size)
    image_region_raw = image_region_raw.astype(np.float64)

    # build a grid to represent this image
    grid = _initialize_grid_2d(image_region, voxel_size_yx)

    # add a gaussian for each local maximum while the RSS decreases
    simulation = np.zeros_like(image_region_raw)
    residual = image_region_raw - simulation
    ssr = np.sum(residual ** 2)
    diff_ssr = -1
    nb_gaussian = 0
    best_simulation = simulation.copy()
    positions_gaussian = []
    while diff_ssr < 0 or nb_gaussian == limit_gaussian:
        position_gaussian = np.argmax(residual)
        positions_gaussian.append(list(grid[:, position_gaussian]))
        simulation += _gaussian_2d(grid=grid,
                                   mu_y=float(positions_gaussian[-1][0]),
                                   mu_x=float(positions_gaussian[-1][1]),
                                   sigma_yx=sigma_yx,
                                   voxel_size_yx=voxel_size_yx,
                                   psf_amplitude=amplitude,
                                   psf_background=background,
                                   precomputed=precomputed_gaussian)
        residual = image_region_raw - simulation
        new_ssr = np.sum(residual ** 2)
        diff_ssr = new_ssr - ssr
        ssr = new_ssr
        nb_gaussian += 1
        background = 0

        if diff_ssr < 0:
            best_simulation = simulation.copy()

    if 1 < nb_gaussian < limit_gaussian:
        positions_gaussian.pop(-1)
    elif nb_gaussian == limit_gaussian:
        warnings.warn("Problem occurs during the decomposition of a dense "
                      "region. More than {0} spots seem to be necessary to "
                      "reproduce the candidate region and decomposition was "
                      "stopped early. Set a higher limit or check a potential "
                      "artifact in the image if you do not expect such a "
                      "large region to be decomposed.".format(limit_gaussian),
                      UserWarning)

    best_simulation = np.reshape(best_simulation, image_region.shape)
    max_value_dtype = np.iinfo(image_region.dtype).max
    best_simulation = np.clip(best_simulation, 0, max_value_dtype)
    best_simulation = best_simulation.astype(image_region.dtype)

    return image_region, best_simulation, positions_gaussian
