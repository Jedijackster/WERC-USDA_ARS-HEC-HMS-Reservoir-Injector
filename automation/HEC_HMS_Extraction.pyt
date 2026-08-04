import arcpy
import os
import csv

class Toolbox(object):
    def __init__(self):
        self.label = "HEC-HMS Extraction Toolbox"
        self.alias = "hechms_extraction"
        self.tools = [ExtractDamCoordinates]

class ExtractDamCoordinates(object):
    def __init__(self):
        self.label = "1. Extract Target Dam Coordinates"
        self.description = "Selects dams, extracts coordinates on the fly, and writes a clean CSV to a specified folder."
        self.canRunInBackground = False

    def getParameterInfo(self):
        # 1. Target Subwatershed Boundary (Polygon)
        param0 = arcpy.Parameter(
            displayName="Target Subwatershed Boundary",
            name="in_boundary",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param0.filter.list = ["Polygon"]

        # 2. Master Dams Layer (Points)
        param1 = arcpy.Parameter(
            displayName="Master Dams Layer",
            name="in_dams",
            datatype="GPFeatureLayer",
            parameterType="Required",
            direction="Input")
        param1.filter.list = ["Point"]

        # 3. Dam Name Field
        param2 = arcpy.Parameter(
            displayName="Dam Name Field",
            name="name_field",
            datatype="Field",
            parameterType="Required",
            direction="Input")
        param2.parameterDependencies = [param1.name]

        # 4. Output Folder Destination
        param3 = arcpy.Parameter(
            displayName="Output Folder",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Input")

        # 5. File Name Input Textbox
        param4 = arcpy.Parameter(
            displayName="CSV File Name",
            name="out_name",
            datatype="GPString",
            parameterType="Required",
            direction="Input")
        param4.value = "reservoirs.csv"  # Default name pre-filled

        return [param0, param1, param2, param3, param4]

    def isLicensed(self):
        return True

    def updateParameters(self, parameters):
        return

    def updateMessages(self, parameters):
        return

    def execute(self, parameters, messages):
        in_boundary = parameters[0].valueAsText
        in_dams = parameters[1].valueAsText
        name_field = parameters[2].valueAsText
        out_folder = parameters[3].valueAsText
        out_name = parameters[4].valueAsText

        # Automatically handle the .csv extension
        if not out_name.lower().endswith(".csv"):
            out_name += ".csv"

        # Combine folder destination and file name into full path
        out_csv = os.path.join(out_folder, out_name)

        try:
            # STEP 1: Select dams that intersect the boundary
            arcpy.AddMessage("Isolating dams within the target boundary...")
            selected_dams = arcpy.management.SelectLayerByLocation(
                in_layer=in_dams,
                overlap_type="INTERSECT",
                select_features=in_boundary,
                selection_type="NEW_SELECTION"
            )

            count = int(arcpy.management.GetCount(selected_dams).getOutput(0))
            if count == 0:
                arcpy.AddError("No dams found within the specified boundary.")
                return
            
            arcpy.AddMessage(f"Successfully isolated {count} target dams.")
            arcpy.AddMessage(f"Creating CSV file at: {out_csv}")

            # STEP 2: Use SearchCursor and write directly to destination
            sr_wgs84 = arcpy.SpatialReference(4326)
            fields_to_read = [name_field, "SHAPE@X", "SHAPE@Y"]
            
            with open(out_csv, mode='w', newline='', encoding='utf-8') as csv_file:
                csv_writer = csv.writer(csv_file)
                
                # Write header
                csv_writer.writerow(["Dam_Name", "Canvas_X", "Canvas_Y"])
                
                # Extract coordinates and write rows
                with arcpy.da.SearchCursor(selected_dams, fields_to_read, spatial_reference=sr_wgs84) as cursor:
                    for row in cursor:
                        dam_name = row[0]
                        canvas_x = row[1]  # Longitude
                        canvas_y = row[2]  # Latitude
                        
                        csv_writer.writerow([dam_name, canvas_x, canvas_y])

            arcpy.AddMessage("Extraction Complete! CSV successfully created.")

            # Clear selection
            arcpy.management.SelectLayerByAttribute(in_dams, "CLEAR_SELECTION")

        except arcpy.ExecuteError:
            arcpy.AddError(arcpy.GetMessages(2))
        except Exception as e:
            arcpy.AddError(str(e))