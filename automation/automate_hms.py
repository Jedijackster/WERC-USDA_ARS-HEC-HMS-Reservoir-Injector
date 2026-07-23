import os
import csv

def automate_reservoir_injection(basin_file, csv_file):
    # 1. Read the reservoir data from the CSV
    reservoirs = []
    with open(csv_file, mode='r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Clean up key names and values (strip surrounding whitespace)
            cleaned_row = {k.strip(): v.strip() for k, v in row.items() if k}
            reservoirs.append(cleaned_row)
            
    # 2. Read the existing basin file content
    with open(basin_file, 'r') as f:
        content = f.read()

    # 3. Build dynamic text blocks for each reservoir in the CSV
    new_blocks = []
    for res in reservoirs:
        name = res.get('Name')
        
        # Read raw input values
        val_1 = res.get('Canvas_X') or res.get('Canvas X') or res.get('X') or '0.0'
        val_2 = res.get('Canvas_Y') or res.get('Canvas Y') or res.get('Y') or '0.0'
        
        # Detect and swap if Latitude (positive ~30-40) was placed in Canvas_X 
        # and Longitude (negative ~ -90 to -105) was placed in Canvas_Y
        try:
            f1, f2 = float(val_1), float(val_2)
            if f1 > 0 and f2 < 0:
                x_coord = f2  # Longitude (X)
                y_coord = f1  # Latitude (Y)
            else:
                x_coord = f1
                y_coord = f2
        except ValueError:
            x_coord, y_coord = val_1, val_2
        
        downstream = res.get('Downstream_Element', '')
        init_elev = res.get('Initial_Elev', '')

        # Format HEC-HMS block syntax (5 leading spaces required for parameters)
        block = f"Reservoir: {name}\n"
        block += f"     Canvas X: {x_coord}\n"
        block += f"     Canvas Y: {y_coord}\n"
        block += f"     Route Method: Outflow Curve\n"
        block += f"     Storage Method: Elevation-Storage\n"
        block += f"     Initial Condition: Elevation\n"
        if init_elev:
            block += f"     Initial Elevation: {init_elev}\n"
        if downstream and downstream.lower() != 'none':
            block += f"     Downstream: {downstream}\n"
        block += "End:\n\n"

        new_blocks.append(block)

    # 4. Append new reservoir blocks to the basin content
    if not content.endswith('\n'):
        content += '\n'
        
    for block in new_blocks:
        content += block

    # 5. Overwrite the .basin file
    with open(basin_file, 'w') as f:
        f.write(content)
        
    print(f"Successfully updated and inserted {len(reservoirs)} reservoir elements into {basin_file}.")
    
    
if __name__ == "__main__":
    BASIN_PATH = r"C:\Users\jay6627\Downloads\Calibration_2_after_correction 1\Calibration_2_after_correction\Basin_1___Copy__1_.basin"
    CSV_PATH = r"reservoirs.csv"
    
    # Executes the function with your specified file paths
    automate_reservoir_injection(BASIN_PATH, CSV_PATH)