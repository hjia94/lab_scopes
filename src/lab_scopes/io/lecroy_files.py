# -*- coding: utf-8 -*-
# created by Jia Han, Sep-16-2019
# Read Lecroy scope data files in .trc (binary) or .txt (ascii)

'''
Note: when saving on scope, need to choose binary with word format

read_trc_data(file path) => signal, time array
	read binary file with open()
	first 11 bytes is an IEEE 488.2 TMC #9NNNNNNNNN block-length prefix
	WAVEDESC (346-byte waveform descriptor) is in the next 346 bytes; decoded by lab_scopes.lecroy.wavedesc.LeCroyWavedesc
	data bytes are decoded using struct.unpack(). Each data point uses 2 bytes. Real voltage value is converted using WAVEDESC info
---------------------------------------------------------------------------
Note: when saving on scope, need to choose ascii and ',' as deliminator

read_txt_data(file path) => signal, time array
	use open() to look at first 5 rows and print out when and which scope data was taken from
	use numpy.loadtxt() to read the data and time array
---------------------------------------------------------------------------
compare_trigger_times(file path1, file path2) => True/False
	compare the trigger time information in the WAVEDESC of two .trc files
	return True if they are the same, False if not
--------------------------------------------------------------------------
read_hdf5_scope_tarr(f, scope_name) => time array
	Read the time array for a given scope group from an open HDF5 file.
--------------------------------------------------------------------------
read_hdf5_scope_data(f, scope_name, channel_name, shot_number) => voltage data array
	Read and convert raw scope channel data for a given shot from an open HDF5 file.
--------------------------------------------------------------------------
read_hdf5_all_scopes_channels(f, shot_number, include_tarr=True) => dict of all scope data
	Read all channel data for all scope groups for a given shot from an open HDF5 file
--------------------------------------------------------------------------

Feb.2024 update:
1. Variable Naming: Used snake_case for variable names, following PEP8 conventions.
2. Directly converted the unpacked data to a NumPy array and performed data manipulation in NumPy for efficiency.
3. Ensured consistent use of string formatting.

Sep.2025 update:
Added functions to read scope data from HDF5 files written by LAPD_DAQ, including reading time arrays and channel data for specific shots.
'''

import numpy as np

from lab_scopes.lecroy import LeCroyWavedesc

# .trc files start with a TMC-style block-length prefix "#9NNNNNNNNN" (11 bytes)
# declaring the byte count of the WAVEDESC + sample payload that follows.
TRC_BLOCK_PREFIX_BYTES = 11
WAVEDESC_BYTES = 346
TRACE_DATA_OFFSET = TRC_BLOCK_PREFIX_BYTES + WAVEDESC_BYTES
TRACE_SAMPLE_DTYPE = np.dtype("=i2")

#======================================================================================

def decode_wavedesc(wavedesc_bytes):
	try:
		wavedesc = LeCroyWavedesc(wavedesc_bytes)
	except Exception as e:
		print("Error decoding LeCroyWavedesc info:", e)
		wavedesc = None
	return wavedesc

#======================================================================================

def _read_trace_bytes(file_path):
	with open(file_path, mode='rb') as file:
		return file.read()

def _scale_trace_data(file_content, data_size, vertical_gain, vertical_offset):
	raw_data = np.frombuffer(
		file_content,
		dtype=TRACE_SAMPLE_DTYPE,
		count=data_size,
		offset=TRACE_DATA_OFFSET,
	)
	return raw_data.astype(np.float64, copy=False) * vertical_gain - vertical_offset

def _h5py():
	try:
		import h5py
	except ImportError as exc:
		raise ImportError("Install lab-scopes[hdf5] to use HDF5 reader helpers.") from exc
	return h5py

#======================================================================================

def open_hdf5_readonly(path):
	"""Open an HDF5 archive read-only without contending for the file lock.

	Returns an open ``h5py.File`` the caller must close (use as a context
	manager). The point of this helper is so multiple analysis scripts -- and
	the live DAQ writer -- can touch the same file at once without blocking each
	other or raising "unable to lock file" on Windows, where the HDF5 file lock
	is mandatory.

	Strategy, most-cooperative first:
	  1. ``swmr=True`` -- Single-Writer/Multiple-Reader. Readers take no lock that
	     conflicts with an appending writer, and see a consistent view. Requires
	     the file to have been written with ``libver='latest'``.
	  2. ``locking='best-effort'`` -- newer h5py/HDF5: open read-only but don't
	     fail if the lock can't be taken (older files not written for SWMR).
	  3. plain ``'r'`` -- last resort for ancient builds without ``locking``.

	All paths open read-only, so none of them can ever modify or corrupt the
	file; the fallbacks only affect *locking* behavior, not the bytes read.
	"""
	h5py = _h5py()
	try:
		return h5py.File(path, "r", swmr=True)
	except Exception:
		pass
	try:
		return h5py.File(path, "r", locking="best-effort")
	except (TypeError, ValueError):
		# Old h5py without the `locking` kwarg.
		return h5py.File(path, "r")

#======================================================================================

def get_trigger_time(file_path):
	"""
	Extract trigger timing information from LeCroy scope file header.
	
	Parameters:
	-----------
	file_path : str
		Path to the .trc file
		
	Returns:
	--------
	datetime
		datetime object of when the trace was triggered
	"""
	
	file_content = _read_trace_bytes(file_path)
	
	first_11 = file_content[:11].decode()
	if not first_11.startswith('#9'):
		raise SyntaxError('First two bytes are not #9')
	
	wavedesc_bytes = file_content[11:11+346]
	wavedesc = decode_wavedesc(wavedesc_bytes)

	if wavedesc is None:
		raise ValueError("Could not decode WAVEDESC information")

	# Extract trigger time components from WAVEDESC
	# Note: LeCroy stores year as offset from 1900
	year = wavedesc.wd.tt_year
	month = wavedesc.wd.tt_months
	day = wavedesc.wd.tt_days
	hour = wavedesc.wd.tt_hours
	minute = wavedesc.wd.tt_minute
	second = wavedesc.wd.tt_second
	
	
	return {'year': year, 'month': month, 'day': day, 'hour': hour, 'minute': minute, 'second': second}

#======================================================================================

def compare_trigger_times(file_path1, file_path2, debug=False):
	"""
	Compare trigger times of two LeCroy scope files.
	
	Parameters:
	-----------
	file_path1 : str
		Path to the first .trc file
	file_path2 : str
		Path to the second .trc file
	tolerance_seconds : float, optional
		Maximum difference in seconds to consider times as "same" (default: 1 second)
		
	Returns:
	--------
	bool
		True if trigger times are the same (within tolerance), False otherwise
	"""
	
	try:
		time1 = get_trigger_time(file_path1)
		time2 = get_trigger_time(file_path2)
		
		if time1['year'] != time2['year']: 
			return False
		elif time1['month'] != time2['month']: 
			return False
		elif time1['day'] != time2['day']: 
			return False
		elif time1['hour'] != time2['hour']: 
			return False
		elif time1['minute'] != time2['minute'] or time1['second'] != time2['second']:
			if debug:
				# Calculate total seconds for both times (minute*60 + second)
				total_seconds1 = time1['minute'] * 60 + time1['second']
				total_seconds2 = time2['minute'] * 60 + time2['second']
				diff_seconds = total_seconds2 - total_seconds1
				print(f'Time difference: {diff_seconds} seconds')
			return False
		else: 
			return True
	except Exception as e:
		print(f"Error comparing trigger times: {e}")
		return False

#======================================================================================

def read_trc_data(file_path, list_some_wavedesc_info=False):

	file_content = _read_trace_bytes(file_path)

	first_11 = file_content[:TRC_BLOCK_PREFIX_BYTES].decode()

	if not first_11.startswith('#9'):
		raise SyntaxError('First two bytes are not #9')

	wavedesc_bytes = file_content[TRC_BLOCK_PREFIX_BYTES:TRACE_DATA_OFFSET]
	wavedesc = decode_wavedesc(wavedesc_bytes)

	data_size = int((int(first_11[2:]) - WAVEDESC_BYTES) / 2)
	if data_size != len(wavedesc.time_array):
		print('Time array length from WAVEDESC %i does not equal %i from first 11 bytes' %(len(wavedesc.time_array), data_size))
	data_size = len(wavedesc.time_array)

	if list_some_wavedesc_info:
		print("dt =", wavedesc.dt)
		print("t0 =", wavedesc.t0)
		print("vertical_gain =", wavedesc.vertical_gain)
		print("timebase =", wavedesc.timebase)
		print("Input = ", wavedesc.vertical_coupling)

	print('Reading data...')
	data = _scale_trace_data(file_content, data_size, wavedesc.wd.vertical_gain, wavedesc.wd.vertical_offset)

	print('Done')

	return data, wavedesc.time_array # signal, time array

#======================================================================================
def read_trc_data_simplified(file_path):

	file_content = _read_trace_bytes(file_path)

	wavedesc_bytes = file_content[TRC_BLOCK_PREFIX_BYTES:TRACE_DATA_OFFSET]
	wavedesc = decode_wavedesc(wavedesc_bytes)
	data_size = len(wavedesc.time_array)

	data = _scale_trace_data(file_content, data_size, wavedesc.wd.vertical_gain, wavedesc.wd.vertical_offset)

	return data, wavedesc.time_array, wavedesc.wd.vertical_gain, wavedesc.wd.vertical_offset

def read_trc_data_no_wavedesc(file_path, data_size, vertical_gain, vertical_offset):
	file_content = _read_trace_bytes(file_path)
	return _scale_trace_data(file_content, data_size, vertical_gain, vertical_offset)


# Deprecated aliases retained for one release for external callers.
decode_header_info = decode_wavedesc
read_trc_data_no_header = read_trc_data_no_wavedesc

#======================================================================================

def read_txt_data(ifn):

	with open(ifn, "r") as file:
		file_content = file.read()
		
		if 'Segment' not in file_content[:50]:
			print('First 5 rows might include data. Check on text reader before using this function to read.')

		print(file_content[:15], ' trace saved on', file_content[100:119])


	data = np.loadtxt(ifn,dtype=float, delimiter=',', skiprows=5)

	print('Done')

	return data[:,1], data[:,0] - data[0,0] # signal, time array

#======================================================================================

def read_hdf5_scope_tarr(f, scope_name):
	"""
	Read the time array for a given scope group from an open HDF5 file.

	Parameters
	----------
	f : h5py.File
		Open HDF5 file object (not a filename)
	scope_name : str
		Name of the scope group (e.g., 'bdotscope', 'xrayscope')

	Returns
	-------
	np.ndarray
		Time array for the specified scope group

	Raises
	------
	KeyError
		If the scope group or time array is not found
	"""
	if scope_name not in f:
		raise KeyError(f"Scope group '{scope_name}' not found in HDF5 file")
	scope_group = f[scope_name]
	if 'time_array' not in scope_group:
		raise KeyError(f"Time array not found for scope '{scope_name}'")
	return scope_group['time_array'][:]

#======================================================================================

def read_hdf5_scope_data(f, scope_name, channel_name, shot_number):
	"""
	Read and convert raw scope channel data for a given shot from an open HDF5 file.

	Parameters
	----------
	f : h5py.File
		Open HDF5 file object (not a filename)
	scope_name : str
		Name of the scope group (e.g., 'bdotscope', 'xrayscope')
	channel_name : str
		Name of the channel (e.g., 'C1', 'C2')
	shot_number : int
		Shot number to read (e.g., 1)

	Returns
	-------
	np.ndarray
		Calibrated voltage data for the specified channel

	Raises
	------
	KeyError
		If the group or dataset is missing
	ValueError
		If the shot is marked as skipped or WAVEDESC cannot be decoded
	"""

	# Fast local lookups
	try:
		scope_group = f[scope_name]
		shot_group = scope_group[f'shot_{shot_number}']
	except KeyError as e:
		raise KeyError(f"Missing group: {e}")

	attrs = shot_group.attrs
	if attrs.get('skipped', False):
		raise ValueError(f"Shot {shot_number} was skipped. Reason: {attrs.get('skip_reason', 'Unknown reason')}")

	data_key = f'{channel_name}_data'
	# LAPD_DAQ writes the WAVEDESC bytes under "<channel>_header" on disk; keep the
	# legacy dataset name for backward compatibility with existing HDF5 archives.
	wavedesc_key = f'{channel_name}_header'
	try:
		raw_data = shot_group[data_key][:]
		wavedesc_bytes = shot_group[wavedesc_key][()]
	except KeyError as e:
		raise KeyError(f"Missing dataset: {e}")

	wavedesc = decode_wavedesc(wavedesc_bytes)
	if wavedesc is None:
		raise ValueError(f"Could not decode WAVEDESC for {scope_name}/shot_{shot_number}/{channel_name}")

	# Vectorized conversion
	gain = wavedesc.wd.vertical_gain
	offset = wavedesc.wd.vertical_offset
	voltage_data = raw_data.astype(np.float64) * gain - offset
	return voltage_data, wavedesc.dt, wavedesc.t0

#======================================================================================

def read_hdf5_all_scopes_channels(f, shot_number, include_tarr=True):
	"""
	Read all channel data for all scope groups for a given shot from an open HDF5 file.

	Parameters
	----------
	f : h5py.File
		Open HDF5 file object (not a filename)
	shot_number : int
		Shot number to load (e.g., 1 => group 'shot_1')
	include_tarr : bool, optional
		If True, include the scope time array in the result under 'time_array'.
		If False, the 'time_array' value will be None. Default True.

	Returns
	-------
	dict
		Nested dictionary of the form:
		{
		  scope_name: {
			'time_array': np.ndarray | None,
			'channels': {
			   channel_name: np.ndarray  # voltage data
			}
		  },
		  ...
		}
	"""
	h5py = _h5py()
	result = {}

	skip_groups = {'Configuration', 'Control'}
	for scope_name, scope_group in f.items():
		if scope_name in skip_groups:
			continue
		else:
			result[scope_name] = {}
		shot_group_name = f'shot_{shot_number}'
		if shot_group_name not in scope_group:
			print(f"Scope '{scope_name}' is not recorded for shot '{shot_number}'")
			continue
		shot_group = scope_group[shot_group_name]
		attrs = shot_group.attrs
		if attrs.get('skipped', False):
			print(f"Shot {shot_number} for scope '{scope_name}' was skipped: {attrs.get('skip_reason', 'Unknown reason')}")
			continue

		channels = {}
		for key, ds in shot_group.items():
			if not (isinstance(ds, h5py.Dataset) and key.endswith('_data')):
				continue
			channel_name = key[:-5]
			data, dt, t0 = read_hdf5_scope_data(f, scope_name, channel_name, shot_number)
			channels[channel_name] = data
		result[scope_name]['channels'] = channels

		if include_tarr:
			try:
				tarr = read_hdf5_scope_tarr(f, scope_name)
				if len(tarr) != len(data):
					tarr = np.arange(len(data)) * dt + t0
				result[scope_name]['time_array'] = tarr
			except Exception as e:
				print(f"Could not read time array for scope '{scope_name}': {e}")
				tarr = None

	return result

#======================================================================================

def read_scope_channel_descriptions(f, scope_name):
	"""
	Return a dictionary of channel descriptions for a given scope group from an open HDF5 file.
	"""
	description_dict = {}

	if scope_name not in f:
		print(f"Scope '{scope_name}' not found in file.")
		return description_dict

	current_shot = f[scope_name]['shot_1']

	# Extract channel names and their descriptions
	channel_names = [k.split('_')[0] for k in current_shot.keys() if k.endswith('_data')]

	# Print channel information
	for channel in sorted(channel_names):
		data_key = f"{channel}_data"
		if data_key in current_shot:
			if 'description' in current_shot[data_key].attrs:
				desc = current_shot[data_key].attrs['description']
				description_dict[channel] = desc
			else:
				description_dict[channel] = "No description available"

	return description_dict
