#! /usr/bin.env python

"""This module reads in a Mirage-formatted source catalog and for each
source, interpolates the provided magnitudes in order to produce a
continuum spectrum. The collection of all sources' spectra are then
saved to an hdf5 file in the proper format to be used by the
NIRCAM_Gsim disperser software.

Authors
-------

    - Bryan Hilbert

Use
---

    This script is intended to be executed as such:

    ::

        from mirage.catalogs import spectra_from_catalog
        spectra_dict = spectra_from_catalog.make_all_spectra('my_catalog_file.cat')
"""
from collections import OrderedDict
import copy
import os
import pkg_resources

from astropy.io import ascii
import astropy.units as u
import numpy as np
from scipy.interpolate import interp1d

from . import hdf5_catalog
from mirage.utils.constants import FLAMBDA_CGS_UNITS, FNU_CGS_UNITS
from mirage.utils.utils import magnitude_to_countrate

MODULE_PATH = pkg_resources.resource_filename('mirage', '')
CONFIG_PATH = os.path.join(MODULE_PATH, 'config')
ZEROPOINT_FILES = {'niriss': os.path.join(CONFIG_PATH, 'niriss_zeropoints.list'),
                   'nircam': os.path.join(CONFIG_PATH, 'NIRCam_zeropoints.list'),
                   'fgs': os.path.join(CONFIG_PATH, 'guider_zeropoints.list')}


def add_flam_columns(cat, mag_sys):
    """Convert magnitudes to flux densities in units of f_lambda and add
    to catalog.

    Parameters
    ----------
    cat : astropy.table.Table
        Catalog table (output from read_catalog)

    mag_sys : str
        Magnitude system of the data (e.g. 'abmag')

    Returns
    -------
    cat : astropy.table.Table
        Modified table with "<instrument>_<filter>_flam" columns added

    parameters : dict
        Dictionary of tuples, where each tuple contains the photflam,
        photfnu, zeropoint, and pivot wavelength for a filter
    """
    # Get all of the magnitude columns
    magnitude_columns = [col for col in cat.colnames if 'magnitude' in col]

    # Organize magnitude columns by instrument to minimize file i/o when
    # getting photflam and zeropoint values
    nircam_mag_cols = []
    niriss_mag_cols = []
    fgs_mag_cols = []
    for col in magnitude_columns:
        if 'nircam' in col.lower():
            nircam_mag_cols.append(col)
        elif 'niriss' in col.lower():
            niriss_mag_cols.append(col)
        elif 'fgs' in col.lower():
            fgs_mag_cols.append(col)

    # Get PHOTFLAM and filter zeropoint values
    parameters = {}
    if len(nircam_mag_cols) > 0:
        nrc_params = get_filter_info(nircam_mag_cols, mag_sys)
        parameters = {**parameters, **nrc_params}
    if len(niriss_mag_cols) > 0:
        nrs_params = get_filter_info(niriss_mag_cols, mag_sys)
        parameters = {**parameters, **nrs_params}
    if len(fgs_mag_cols) > 0:
        fgs_params = get_filter_info(fgs_mag_cols, mag_sys)
        parameters = {**parameters, **fgs_params}

    # Create a corresponding f_lambda column for each magnitude column
    for key in parameters:
        tmp_adjusted_key = key.split('_mod')[0]
        magnitude_values = cat[tmp_adjusted_key].data
        flam_values = convert_to_flam(magnitude_values, parameters[key], mag_sys)
        new_column_name = key.replace('magnitude', 'flam')
        cat[new_column_name] = flam_values
    return cat, parameters


def convert_to_flam(magnitudes, param_tuple, magnitude_system):
    """Convert the magnitude values for a given magnitude column into
    units of f_lambda.

    Parameters
    ----------
    magnitudes : list
        List of magnitude values to be converted

    param_tuple : tup
        Tuple of (photflam, photfnu, zeropoint, pivot_wavelength) for the
        filter corresponding to the input magnitudes

    magnitude_system : str
        Name of the magnitude system for the input magnitudes (e.g. 'abmag')

    Returns
    -------
    flam : list
        List of f_lambda values corresponding to the list of input magnitudes
    """
    photflam, photfnu, zeropoint, pivot = param_tuple

    if magnitude_system in ['stmag', 'vegamag']:
        countrate = magnitude_to_countrate('wfss', magnitude_system, magnitudes, photfnu=photfnu,
                                           photflam=photflam, vegamag_zeropoint=zeropoint)
        flam = countrate * photflam

    if magnitude_system == 'abmag':
        flam = 1. / (3.34e4 * pivot.to(u.AA).value**2) * 10**(-0.4*(magnitudes-8.9))

    return flam


def create_output_sed_filename(catalog_file, spec_file):
    """Create a name for the output hdf5 file continaing object SEDs given
    the input ascii source catalog filename and optionally the name of an
    input hdf5 file containing object spectra.

    Parameters
    ----------
    catalog_file : str
        Name of ascii source catalog file

    spec_file : str
        Name of hdf5 file containing object spectra. If None it is ignored

    Returns
    -------
    sed_file : str
        Name of hdf5 file to contain object SEDs
    """
    in_dir, in_file = os.path.split(catalog_file)
    last_dot = in_file.rfind('.')
    in_base = in_file[0: last_dot]

    if spec_file is not None:
        in_spec_dir, in_spec_file = os.path.split(spec_file)
        dot = in_spec_file.rfind('.')
        in_spec_base = in_spec_file[0: dot]
        sed_file = "source_sed_file_from_{}_and_{}.hdf5".format(in_base, in_spec_base)
    else:
        sed_file = "source_sed_file_from_{}.hdf5".format(in_base)
    sed_file = os.path.join(in_dir, sed_file)
    return sed_file


def create_spectra(catalog_with_flambda, filter_params, extrapolate_SED=True):
    """Create object spectra from an astropy Table source catalog

    Parameters
    ----------
    catalog_with_flambda : astropy.table.Table
        Source catalog containing f_lambda columns (i.e. output from
        add_flam_columns)

    filter_params : tup
        Tuple of photometric information for a filter.
        (photflam, photfnu, zeropoint, pivot wavelength)

    extrapolate_SED : bool
        If True and an input SED does not cover the entire wavelength range
        of the grism, linear interpolation is used to extend the SED

    Returns
    -------
    spectra : dict
        Object spectra in a dictionary. Keys are index numbers, values are
        dictionaries containing 'wavelengths' and 'fluxes' keys. Those values
        are numpy arrays of values. These can optionally have astropy units
        attached to them.
    """
    flambda_cols = [col for col in catalog_with_flambda.colnames if 'flam' in col]
    filter_name = [colname.split('_')[1] for colname in flambda_cols]
    instrument = np.array([colname.split('_')[0] for colname in flambda_cols])
    min_wave = 0.9  # microns
    max_wave = 5.15  # microns
    if np.all(instrument == 'nircam'):
        min_wave = 2.35  # microns

    pivots = []
    for column in flambda_cols:
        pivot_column = column.replace('flam', 'magnitude')
        f_lambda, f_nu, zeropoint, pivot = filter_params[pivot_column]
        pivots.append(pivot.value)
    pivots = np.array(pivots)

    if (len(pivots) == 1):
        print(("INFO: single filter magnitude input. Extrapolating to produce "
               "a flat continuum."))
        extrapolate_SED = True
        pivots = np.append(pivots, pivots[0] + 0.01)

    # Put the pivot wavelengths into increasing order
    sorted_indexes = np.argsort(np.array(pivots))
    wavelengths = pivots[sorted_indexes]
    final_wavelengths = copy.deepcopy(wavelengths)
    if (np.min(wavelengths) > min_wave) and extrapolate_SED:
        final_wavelengths = np.insert(final_wavelengths, 0, min_wave)
    if (np.max(wavelengths) < max_wave) and extrapolate_SED:
        final_wavelengths = np.append(final_wavelengths, max_wave)

    spectra = OrderedDict({})
    for idx, source in enumerate(catalog_with_flambda):
        index = source['index']
        flux = []
        for column in flambda_cols:
            flux.append(source[column])
        # Case where a single magnitude is all that's given
        if len(flux) == 1:
            flux.append(flux[0])

        sorted_fluxes = np.array(flux)[sorted_indexes]
        final_sorted_fluxes = copy.deepcopy(sorted_fluxes)

        # If the provided flux values don't cover the complete wavelength
        # range, extrapolate, if requested.
        if ((np.min(wavelengths) > min_wave) or (np.max(wavelengths) < max_wave)) and extrapolate_SED:
            interp_func = interp1d(wavelengths, sorted_fluxes, fill_value="extrapolate", bounds_error=False)
            final_sorted_fluxes = interp_func(final_wavelengths)
        # Set any flux values that are less than zero to zero
        final_sorted_fluxes[final_sorted_fluxes < 0.] = 0.

        spectra[index] = {'wavelengths': final_wavelengths * u.micron,
                          'fluxes': final_sorted_fluxes * FLAMBDA_CGS_UNITS}

    return spectra


def get_filter_info(column_names, magsys):
    """Given a list of catalog columns names (e.g. 'nircam_f480m_magnitude')
    get the corresponding PHOTFLAM, PHOTFNU, filter zeropoint and pivot
    wavelength values

    Parameters
    ----------
    column_names : list
        List of Mirage catalog column names

    magsys : str
        Magnitude system (e.g. 'abmag')

    Returns
    -------
    info : dict
        Dictionary of values
        (e.g. info['nircam_f480m_magnitude'] = (<photflam>, <photfnu>,
        <zeropoint>, <pivot>))
    """
    # Get the correct zeropoint file name
    instrument = column_names[0].split('_')[0].lower()
    zp_file = ZEROPOINT_FILES[instrument]

    # Read in the file
    zp_table = ascii.read(zp_file)

    magsys = magsys.upper()
    info = {}

    if instrument in ['nircam', 'niriss']:
        for entry in column_names:
            filter_name = entry.split('_')[1].upper()
            if instrument == 'nircam':
                match = ((zp_table['Filter'] == filter_name) & (zp_table['Module'] == 'B'))
            elif instrument == 'niriss':
                match = zp_table['Filter'] == filter_name
            zp = zp_table[magsys].data[match][0]
            photflam = zp_table['PHOTFLAM'].data[match][0] * FLAMBDA_CGS_UNITS
            photfnu = zp_table['PHOTFNU'].data[match][0] * FNU_CGS_UNITS
            pivot = zp_table['Pivot_wave'].data[match][0] * u.micron
            info[entry] = (photflam, photfnu, zp, pivot)

    # For FGS, just use the values for GUIDER1 detector.
    elif instrument == 'fgs':
        line = zp_table[0]
        # detector = line['Detector']
        zp = line[magsys]
        photflam = line['PHOTFLAM'] * FLAMBDA_CGS_UNITS
        photfnu = line['PHOTFNU'] * FNU_CGS_UNITS
        pivot = line['Pivot_wave'] * u.micron
        info[column_names[0]] = (photflam, photfnu, zp, pivot)

    return info


def make_all_spectra(catalog_files, input_spectra=None, input_spectra_file=None,
                     extrapolate_SED=True, output_filename=None, normalizing_mag_column=None):
    """Overall wrapper function

    Parameters
    ----------
    catalog_files : str or list
        Name(s) of Mirage-formatted source catalog file(s) (ascii)

    input_spectra : dict
        Dictionary containing spectra for some/all targets. Dictionary
        keys are the object indexes (such as from the index column in
        the catalog_file. Entries must be e.g.
        d[1] = {"wavelengths": <wavelength_list>,
                "fluxes": <List of spectral flux densities>}
        Wavelengths and fluxes can be lists, or lists with astropy units
        attached. If no units are supplied, Mirage assumes wavelengths
        in microns and flux densities in Flambda units.

    input_specctra_file : str
        Name of an hdf5 file containing spectra for some/all targets

    extrapolate_SED : bool
        If True and an input SED does not cover the entire wavelength range
        of the grism, linear interpolation is used to extend the SED

    output_filename : str
        Name of the output HDF5 file, which will contain SEDs for all
        targets.

    normalizing_mag_column : str
        If some/all of the input spectra (from input_spectra or
        input_spectra_file) are normalized (to a median of 1.0), they will
        be scaled based on the magnitude values given in this specified
        column from the input catalog_file.

    Returns
    -------
    output_filename : str
        Name of the saved HDF5 file containing all object spectra.
    """
    # Create the output filename if needed
    if output_filename is None:
        output_filename = create_output_sed_filename(catalog_files[0], input_spectra_file)

    all_input_spectra = {}
    # Read in input spectra from file, add to all_input_spectra dictionary
    if input_spectra_file is not None:
        spectra_from_file = hdf5_catalog.open(input_spectra_file)
        all_input_spectra = {**all_input_spectra, **spectra_from_file}

    # If both an input spectra file and dictionary are given, combine into
    # a single dictionary. Running in this order means that dictionary
    # inputs override inputs from a file.
    if input_spectra is not None:
        all_input_spectra = {**all_input_spectra, **input_spectra}

    # If a single input source catalog is provided, make it a list, in order
    # to be consistent with the case of multiple input catalogs
    if isinstance(catalog_files, str):
        catalog_files = [catalog_files]

    # Loop over input catalogs, which may be of different types
    # (e.g. point source, galaxy, etc)
    for catalog_file in catalog_files:
        # Read in input catalog
        ascii_catalog, mag_sys = read_catalog(catalog_file)

        # Create catalog output name if none is given
        cat_dir, cat_file = os.path.split(catalog_file)
        index = cat_file.rindex('.')
        suffix = cat_file[index:]
        outbase = cat_file[0: index] + '_with_flambda' + suffix
        flambda_output_catalog = os.path.join(cat_dir, outbase)

        catalog, filter_info = add_flam_columns(ascii_catalog, mag_sys)
        catalog.write(flambda_output_catalog, format='ascii', overwrite=True)
        print('Catalog updated with f_lambda columns, saved to: {}'.format(flambda_output_catalog))

        # Remormalize here so you have access to filter_info
        if len(all_input_spectra) > 0 and normalizing_mag_column is not None:
            rescaling_magnitudes = ascii_catalog['index', normalizing_mag_column]
            rescaling_parameters = filter_info[normalizing_mag_column]
            all_input_spectra = rescale_normalized_spectra(all_input_spectra, rescaling_magnitudes,
                                                           rescaling_parameters, mag_sys)

        # For sources in catalog_file but not in all_input_spectra, use the
        # magnitudes in the catalog_file, interpolate/extrapolate as necessary
        # and create continuum spectra
        indexes_to_create = []
        for i, line in enumerate(ascii_catalog):
            if line['index'] not in all_input_spectra.keys():
                indexes_to_create.append(i)

        if len(indexes_to_create) > 0:
            continuum = create_spectra(ascii_catalog[indexes_to_create], filter_info,
                                       extrapolate_SED=extrapolate_SED)
            all_input_spectra = {**all_input_spectra, **continuum}

    # For convenience, reorder the sources by index number
    spectra = OrderedDict({})
    for item in sorted(all_input_spectra.items()):
        spectra[item[0]] = item[1]

    # Save the source spectra in an hdf5 file
    hdf5_catalog.save(spectra, output_filename, wavelength_unit='micron', flux_unit='flam')
    print('Spectra catalog file saved to {}'.format(output_filename))
    return output_filename


def read_catalog(filename):
    """Read in the Mirage-formatted catalog

    Parameters
    ----------
    filename : str
        Name of (ascii) catalog file

    Returns
    -------
    catalog : astropy.table.Table
        Catalog contents

    mag_sys : str
        Magnitude system (e.g. 'abmag', 'stmag', 'vegamag') of the
        source magnitudes in the catalog
    """
    catalog = ascii.read(filename)

    # Check to be sure index column is present
    if 'index' not in catalog.colnames:
        raise ValueError(('WARNING: input catalog must have an index column '
                          'so that object spectra can be associated with the '
                          'correct object in the seed image/segmentation map.'))
    min_index_value = min(catalog['index'].data)
    if min_index_value < 1:
        raise ValueError(("WARNING: input catalog cannot have index values less"
                          "than 1, for the purposes of creating the segmentation map."))

    # Get the magnitude system
    mag_sys = [l.lower() for l in catalog.meta['comments'][0:4] if 'mag' in l][0]
    if mag_sys not in ['abmag', 'stmag', 'vegamag']:
        raise ValueError(('WARNING: unrecognized magnitude system {}. Must be'
                          'one of "abmag", "stmag", or "vegamag".').format(mag_sys))
    return catalog, mag_sys


def rescale_normalized_spectra(spec, catalog_info, filter_parameters, magnitude_system):
    """Rescale any input spectra that are normalized

    Parameters
    ----------
    spec : OrderedDict
        Dictionary containing spectra for some/all targets. Dictionary
        keys are the object indexes (such as from the index column in
        the catalog_file. Entries must be e.g.
        d[1] = {"wavelengths": <wavelength_list>,
                "fluxes": <List of spectral flux densities>}
        Wavelengths and fluxes can be lists, or lists with astropy units
        attached. If no units are supplied, Mirage assumes wavelengths
        in microns and flux densities in Flambda units.


    catalog_info : astropy.table.Table
        Index column and magnitude column to use for rescaling. Extracted
        from the original input catalog.

    filter_parameters : tup
        Photflam, photfnu, zeropoint, pivot wavelength for filter to use
        for rescaling

    magnitude_system : str
        Magnitude system corresponding to the input magnitudes (e.g. 'abmag')

    Returns
    -------
    spec : OrderedDict
        Input dictionary, with flux values rescaled for sources that are
        normalized
    """
    photflam, photfnu, zp, pivot = filter_parameters
    mag_colname = [col for col in catalog_info.colnames if 'index' not in col][0]
    print('Normalizing magnitude column name is {}'.format(mag_colname))

    for dataset in spec:
        waves = spec[dataset]['wavelengths']
        flux = spec[dataset]['fluxes']
        flux_units = flux.unit
        if (flux_units == u.pct):
            # print('SED for source {} is normalized. Rescaling.'.format(dataset))
            match = catalog_info['index'] == dataset
            if not any(match):
                raise ValueError(('WARNING: No matching target in ascii catalog for normalized source '
                                  'number {}. Unable to rescale.').format(dataset))
            magnitude = catalog_info[mag_colname][match]
            mag_in_flam = convert_to_flam(magnitude, filter_parameters, magnitude_system)
            flux *= mag_in_flam
            spec[dataset]['fluxes'] = flux * FLAMBDA_CGS_UNITS
    return spec
