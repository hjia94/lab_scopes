from pathlib import Path

from lab_scopes.io.lecroy_files import read_trc_data_simplified


path = Path(r"D:\data\raw data\C1-interf-shot00000.trc")
data, time_array, gain, offset = read_trc_data_simplified(path)
print(path)
print(f"samples={len(data)} dt={time_array[1] - time_array[0]:.6g} gain={gain} offset={offset}")
