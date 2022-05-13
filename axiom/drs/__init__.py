"""Main entrypoint for DRS processing."""
import os
import argparse
from datetime import datetime
from uuid import uuid4
import xarray as xr
import axiom.utilities as au
import axiom.drs.utilities as adu
from axiom.drs.domain import Domain
import axiom_schemas as axs
import json
import sys
from distributed import Client, LocalCluster
from axiom.config import load_config
from axiom import __version__ as axiom_version
from axiom.exceptions import NoFilesToProcessException, DRSContextInterpolationException
import shutil
from dask.distributed import progress, wait


def consume(json_filepath):
    """Consume a json payload (for message passing)

    Args:
        json_filepath (str): Path to the JSON file.
    """
    logger = au.get_logger(__name__)

    # Check if the file has already been consumed
    consumed_filepath = json_filepath.replace('.json', '.consumed')
    if os.path.isfile(consumed_filepath):
        logger.info(
            f'{json_filepath} has already been consumed and needs to be cleaned up by another process. Terminating.')
        sys.exit()

    # Check if the file is locked
    if au.is_locked(json_filepath):
        logger.info(
            f'{json_filepath} is locked, possibly by another process. Terminating.')
        sys.exit()

    # Lock the file
    au.lock(json_filepath)

    # Convert to dict
    payload = json.loads(open(json_filepath, 'r').read())

    # Process
    process_multi(**payload)

    # Mark consumed by touching another file.
    au.touch(consumed_filepath)

    # Unlock
    au.unlock(json_filepath)


def process(
    input_files,
    output_directory,
    variable,
    project,
    model,
    domain,
    start_year, end_year,
    output_frequency,
    level=None,
    input_resolution=None,
    overwrite=True,
    preprocessor=None,
    postprocessor=None,
    **kwargs
):
    """Method to process a single variable/domain/resolution combination.

    Args:
        input_files (str or list): Globbable string or list of filepaths.
        output_directory (str) : Path from which to build DRS structure.
        variable (str): Variable to process.
        level (numeric or list) : Vertical levels to process.
        project (str): Project metadata to apply (loaded from user config).
        model (str): Model metadata to apply (loaded from user config).
        start_year (int): Start year.
        end_year (int): End year.
        output_frequency (str): Output frequency to process.
        input_resolution (float, optional): Input resolution in km. Leave black to auto-detect from filepaths.
        overwrite (bool): Overwrite the data at the destination. Defaults to True.
        preprocessor (str): Data preprocessor to activate on input data. Defaults to None.
        postprocesser (str): Data postprocess to activate before writing data. Defaults to None.
        **kwargs: Additional keyword arguments used in metadata interpolation.
    """

    # Start the clock
    timer = au.Timer()
    timer.start()

    # Capture what was passed into this method for interpolation context later.
    local_args = locals()

    # Load the logger and configuration
    logger = au.get_logger(__name__)
    config = load_config('drs')

    # Dump the job id if available
    if 'PBS_JOBID' in os.environ.keys():
        jobid = os.getenv('PBS_JOBID')
        logger.info(f'My PBS_JOBID is {jobid}')

    # Get a list of the potential filepaths
    input_files = au.auto_glob(input_files)
    num_files = len(input_files)
    logger.debug(f'{num_files} to consider before filtering.')

    # Filter by those that actually have the variable in the filename.
    if config.filename_filtering['variable']:
        input_files = [
            f for f in input_files if f'{variable}_' in os.path.basename(f)]
        num_files = len(input_files)
        logger.debug(
            f'{num_files} to consider after filename variable filtering.')

    # Filter by those that actually have the year in the filename.
    if config.filename_filtering['year']:
        input_files = [
            f for f in input_files if f'{start_year}' in os.path.basename(f)]
        num_files = len(input_files)
        logger.debug(f'{num_files} to consider after filename year filtering.')

    # Is there anything left to process?
    if len(input_files) == 0:
        raise NoFilesToProcessException()

    # Detect the input resolution if it it not supplied
    if input_resolution is None:
        logger.debug('No input resolution supplied, auto-detecting')
        input_resolution = adu.detect_resolution(input_files)
        logger.debug(f'Input resolution detected as {input_resolution} km')

    # Load project config
    logger.info(f'Loading project config ({project})')
    project = load_config('projects')[project]

    # Load model config
    logger.info(f'Loading model config ({model})')
    model = load_config('models')[model]

    logger.debug(
        'Loading files into distributed memory, this may take some time.')

    # TODO: Remove!!!! This is just to make CCAM work in the short term
    if preprocessor is None and 'ccam' in input_files[0]:
        logger.warn('CCAM preprocessor override used')
        preprocessor = 'ccam'
        postprocessor = 'ccam'

    # Load a preprocessor, if one exists.
    preprocessor = adu.load_preprocessor(preprocessor)
    def preprocess(ds, *args, **kwargs): return preprocessor(ds, **local_args)

    # Account for fixed variables, if defined
    if 'variables_fixed' in project.keys() and variable in project['variables_fixed']:

        # Load just the first file
        ds = xr.open_dataset(input_files[0], engine='h5netcdf')
        ds = preprocess(ds, variable=variable)

    else:
        ds = xr.open_mfdataset(input_files, chunks=dict(
            time=100), preprocess=preprocess, engine='h5netcdf')

    # Persist now, get it on the cluster
    ds = ds.persist()

    # Determine time-invariance
    time_invariant = 'time' not in list(ds.coords.keys())

    # Assemble the context object (order dependent!)
    logger.debug('Assembling interpolation context.')
    context = config.metadata_defaults.copy()

    # Add metadata from the input data
    context.update(ds.attrs)

    # Add user-supplied metadata
    context.update(kwargs)

    # Add project and model metadata
    context.update(project)
    context.update(model)

    # Add additional args
    context.update(local_args)
    context['res_km'] = input_resolution

    # Select the variable from the dataset
    if level:

        # Select each of the levels requested into a new variable.
        for _level in au.pluralise(level):
            ds[f'{variable}{_level}'] = ds[variable].sel(lev=_level, drop=True)

        # Drop the original variable
        ds = ds.drop(variable)

    # Sort the dimensions (fixes domain subsetting)
    logger.debug('Sorting data')
    sort_coords = list()
    for coord in 'time,lev,lat,lon'.split(','):
        if coord in ds.coords.keys():
            sort_coords.append(coord)

    ds = ds.sortby(sort_coords)

    logger.debug('Applying metadata schema')
    schema = axs.load_schema(config['schema'])
    ds = au.apply_schema(ds, schema)

    logger.info(f'Parsing domain {domain}')
    if isinstance(domain, str):

        # Registered domain
        if adu.is_registered_domain(domain):
            domain = adu.get_domain(domain)

        # Attempt to parse
        else:
            domain = Domain.from_directive(domain)

    # We will only otherwise accept a domain object.
    elif isinstance(domain, Domain) == False:
        raise Exception(f'Unable to parse domain {domain}.')

    logger.debug('Domain: ' + domain.to_directive())

    # Subset the geographical domain
    logger.debug('Subsetting geographical domain.')
    ds = domain.subset_xarray(ds, drop=True)

    # TODO: Need to find a less manual way o do this.
    for year in generate_years_list(start_year, end_year):

        logger.info(f'Processing {year}')

        # Subset the data into just this year
        if not time_invariant:
            _ds = ds.where(ds['time.year'] == year, drop=True)
        else:
            _ds = ds.copy()

        # Historical cutoff is defined in $HOME/.axiom/drs.json
        context['experiment'] = 'historical' if year < config.historical_cutoff else context['rcp']

        native_frequency = adu.detect_input_frequency(_ds)
        logger.info(f'Native frequency of data detected as {native_frequency}')

        # Automatically detect the output_frequency from the input data, this will not require resampling
        if output_frequency == 'from_input' or output_frequency == native_frequency:
            output_frequency = adu.detect_input_frequency(_ds)
            logger.info(
                f'output_frequency detected from inputs ({output_frequency})')
            logger.info(f'No need to resample.')
            # Map the frequency to something DRS-compliant
            context['frequency_mapping'] = config['frequency_mapping'][output_frequency]

        # Fixed variables, just change the frequency_mapping
        elif adu.is_time_invariant(_ds):
            output_frequency = 'fx'
            logger.info(
                'Data is time-invariant (fixed variable), overriding frequency_mapping to fx')
            context['frequency_mapping'] = 'fx'

        # Actually perform the resample
        else:
            logger.debug(f'Resampling to {output_frequency} mean.')
            context['frequency_mapping'] = config['frequency_mapping'][output_frequency]
            _ds = _ds.resample(time=output_frequency).mean()

        # Start persisting the computation now
        _ds = _ds.persist()

        # TODO: Add cell methods?

        # Monthly data should have the days truncated
        context['start_date'] = f'{year}0101' if output_frequency[-1] != 'M' else f'{year}01'
        context['end_date'] = f'{year}1231' if output_frequency[-1] != 'M' else f'{year}12'

        # Tracking info
        context['created'] = datetime.utcnow()
        context['uuid'] = uuid4()

        # Interpolate context
        logger.info('Interpolating context.')
        context = adu.interpolate_context(context)

        # Assemble the global meta, add axiom details
        logger.debug('Assembling global metadata.')
        global_attrs = dict(
            axiom_version=axiom_version,
            axiom_schemas_version=axs.__version__,
            axiom_schema=config.schema
        )

        for key, value in config.metadata_defaults.items():
            global_attrs[key] = str(value) % context

        # Strip and reapply metadata
        logger.debug('Applying metadata')
        _ds.attrs = global_attrs

        # Add in the variable to the context
        context['variable'] = variable

        # Reapply the schema
        logger.info('Reapplying schema')
        _ds = au.apply_schema(_ds, schema)

        # Copy coordinate attributes straight off the inputs
        if config.copy_coordinates_from_inputs:
            for coord in list(_ds.coords.keys()):
                _ds[coord].attrs = ds[coord].attrs

        # Get the full output filepath with string interpolation
        logger.debug('Working out output paths')
        drs_path = adu.get_template(config, 'drs_path') % context
        output_filename = adu.get_template(config, 'filename') % context
        output_filepath = os.path.join(
            output_directory, drs_path, output_filename)
        logger.debug(f'output_filepath = {output_filepath}')

        # Skip if already there and overwrite is not set, otherwise continue
        if os.path.isfile(output_filepath) and overwrite == False:
            logger.debug(
                f'{output_filepath} exists and overwrite is set to False, skipping.')
            continue

        # Check for uninterpolated keys in the output path, which should fail at this point.
        uninterpolated_keys = adu.get_uninterpolated_placeholders(
            output_filepath)
        if len(uninterpolated_keys) > 0:
            logger.error('Uninterpolated keys remain in the output filepath.')
            logger.error(f'output_filepath = {output_filepath}')
            raise DRSContextInterpolationException(uninterpolated_keys)

        # Create the output directory
        output_dir = os.path.dirname(output_filepath)
        logger.debug(f'Creating {output_dir}')
        os.makedirs(output_dir, exist_ok=True)

        # Assemble the encoding dictionaries (to ensure time units work!)
        logger.debug('Applying encoding')
        encoding = dict()

        for coord in list(_ds.coords.keys()):
            if coord not in config.encoding.keys():
                logger.warn(
                    f'Coordinate {coord} is not specified in drs.json file, omitting encoding.')
                continue
            encoding[coord] = config.encoding[coord]

        # Apply a blanket variable encoding.
        encoding[variable] = config.encoding['variables']

        # Postprocess data if required
        postprocessor = adu.load_postprocessor(postprocessor)
        def postprocess(ds, *args, **kwargs): return postprocessor(ds, **local_args)
        _ds = postprocess(_ds)

        # Write to temp file in memory, then move (performance)
        # if config.
        # TODO: To be implemented later
        # tmp_filepath = os.path.join(
        #     os.getenv('PBS_JOBFS'),
        #     os.path.basename(output_filepath)
        # )

        # logger.info('Rechunking data for speed')
        # _ds[variable] = _ds[variable].chunk(dict(time=100))

        logger.info('Waiting for computations to finish.')
        progress(_ds)

        logger.debug(f'Writing {output_filepath}')
        write = _ds.to_netcdf(
            output_filepath,
            format='NETCDF4',
            encoding=encoding,
            unlimited_dims=['time']
        )

    elapsed_time = timer.stop()
    logger.info(f'DRS processing task took {elapsed_time} seconds.')


def generate_years_list(start_year, end_year):
    """Generate a list of years (decades) to process.

    Args:
        start_year (int): Start year.
        end_year (int): End year.

    Returns:
        iterator : Years to process.
    """
    return range(start_year, end_year+1, 10)


def load_variable_config(project_config):
    """Extract the variable configuration out of the project configuration.

    Args:
        project_config (dict-like): Project configuration.

    Returns:
        dict: Variable dictionary with name: [levels] (single level will have a list containing None.)
    """

    # Extract the different rank variables
    v2ds = project_config['variables_2d']
    v3ds = project_config['variables_3d']

    # Create a dictionary of variables to process keyed to an empty list of levels for 2D
    variables = {v2d: [None] for v2d in v2ds}

    # Add in the 3D variables, with levels this time
    for v3d, levels in v3ds.items():
        variables[v3d] = levels

    return variables


def process_multi(variables, domain, project, **kwargs):

    logger = au.get_logger(__name__)

    # Load the project metadata
    project_config = load_config('projects')[project]
    config = load_config('drs')

    # Load all variables if nothing was supplied
    if not variables:
        schema_file = config['schema']
        logger.info(f'No variables supplied, loading from schema as defined in configuration ({schema_file}).')
        schema = axs.load_schema(schema_file)
        variables = list(schema['variables'].keys())
    else:
        logger.debug('User has supplied the following variables')
        logger.debug(variables)

    num_variables = len(variables)
    logger.info(f'{num_variables} variable(s) to process.')

    # Start the cluster if requested
    if config.dask['enable']:
        logger.info('Starting dask client.')

        cluster_config = config.dask['cluster']

        # Add PBS_JOBFS if set
        if 'PBS_JOBFS' in os.environ.keys():
            cluster_config['local_directory'] = os.getenv('PBS_JOBFS')

        cluster = LocalCluster(**cluster_config)
        client = Client(cluster)
        logger.info(client)

    output_frequencies = au.pluralise(kwargs['output_frequency'])
    
    # Yes this is a nested loop, but a single variable/domain/output_freq combination could still be 10K+ files, which WILL be processed in parallel.
    for variable in variables:

        for output_frequency in output_frequencies:

            try:
                # for level in levels:
                logger.info(f'Processing {variable} {output_frequency}')
                instance_kwargs = kwargs.copy()
                instance_kwargs['variable'] = variable
                instance_kwargs['domain'] = domain
                instance_kwargs['project'] = project
                instance_kwargs['output_frequency'] = output_frequency

                # instance_kwargs['overwrite'] = False

                process(**instance_kwargs)

            except NoFilesToProcessException as ex:

                logger.info(f'No files to process for {variable}')

            except Exception as ex:

                logger.error(
                    f'Variable {variable} failed for output_frequency {output_frequency}. Error to follow')
                logger.exception(ex)

                # TODO: Add to failed list?
