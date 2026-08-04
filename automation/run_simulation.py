from hms.model import Project
from hms import Hms
import sys

# Main project file path
project_path = "C:/Users/jay6627/Downloads/Calibration_2_after_correction 1/Calibration_2_after_correction/Model_1_2.hms"

run_name = "Simulation_1" 

print("--> HEC-HMS Engine: Opening Project...")
myProject = Project.open(project_path)

print("--> HEC-HMS Engine: Computing Run: " + run_name)
myProject.computeRun(run_name)

print("--> HEC-HMS Engine: Simulation finished successfully.")
myProject.close()
Hms.shutdownEngine()
sys.exit()

