import os
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk


def run_injection():
    basin_path = basin_entry.get().strip()
    csv_path = csv_entry.get().strip()

    # Basic Validation
    if not basin_path or not os.path.exists(basin_path):
        messagebox.showerror(
            "Error", "Please select a valid HEC-HMS .basin file."
        )
        return

    if not csv_path or not os.path.exists(csv_path):
        messagebox.showerror(
            "Error", "Please select a valid reservoirs .csv file."
        )
        return

    try:
        # =========================================================================
        # DROP EXISTING INJECTION LOGIC HERE
        # e.g., call function: automate_reservoir_injection(basin_path, csv_path)
        # =========================================================================

        # Placeholder demo output:
        status_label.config(
            text="Success! Reservoirs injected into basin model.", fg="green"
        )
        messagebox.showinfo(
            "Success", f"Processed:\nBasin: {basin_path}\nCSV: {csv_path}"
        )

    except Exception as e:
        status_label.config(text="Execution Failed.", fg="red")
        messagebox.showerror("Execution Error", f"An error occurred:\n{str(e)}")


def browse_basin():
    filename = filedialog.askopenfilename(
        title="Select HEC-HMS Basin File",
        filetypes=[("Basin Files", "*.basin"), ("All Files", "*.*")],
    )
    if filename:
        basin_entry.delete(0, tk.END)
        basin_entry.insert(0, filename)


def browse_csv():
    filename = filedialog.askopenfilename(
        title="Select Reservoirs Data CSV",
        filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")],
    )
    if filename:
        csv_entry.delete(0, tk.END)
        csv_entry.insert(0, filename)


# --- GUI Setup ---
root = tk.Tk()
root.title("HEC-HMS Reservoir Injector")
root.geometry("650x320")
root.resizable(True, True)

# Main Frame
frame = tk.Frame(root, padx=15, pady=15)
frame.pack(fill=tk.BOTH, expand=True)

# --- Header Section (Logo + Title) ---
header_frame = tk.Frame(frame)
header_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 15))

# Load & Display WERC Logo
logo_img = None
logo_path = "image_99e4bd.png"  # Matche local image filename

if os.path.exists(logo_path):
    raw_img = Image.open(logo_path)
    # Resize keeping aspect ratio (height set to 60px)
    aspect_ratio = raw_img.width / raw_img.height
    new_width = int(60 * aspect_ratio)
    resized_img = raw_img.resize((new_width, 60), Image.Resampling.LANCZOS)
    logo_img = ImageTk.PhotoImage(resized_img)

    logo_label = tk.Label(header_frame, image=logo_img)
    logo_label.pack(side=tk.LEFT, padx=(0, 12))

title_label = tk.Label(
    header_frame,
    text="HEC-HMS Reservoir Injector",
    font=("Helvetica", 14, "bold"),
    anchor="w",
)
title_label.pack(side=tk.LEFT, fill=tk.Y)


# --- File Selection Fields ---

# 1. Basin File Selector
tk.Label(frame, text="HEC-HMS Basin File (.basin):", anchor="w").grid(
    row=1, column=0, sticky="w", pady=(0, 2)
)
basin_entry = tk.Entry(frame, width=48)
basin_entry.grid(row=2, column=0, padx=(0, 5), pady=(0, 10))
btn_basin = tk.Button(frame, text="Browse...", command=browse_basin)
btn_basin.grid(row=2, column=1, pady=(0, 10))

# 2. CSV File Selector
tk.Label(frame, text="Reservoirs Data File (.csv):", anchor="w").grid(
    row=3, column=0, sticky="w", pady=(0, 2)
)
csv_entry = tk.Entry(frame, width=48)
csv_entry.grid(row=4, column=0, padx=(0, 5), pady=(0, 10))
btn_csv = tk.Button(frame, text="Browse...", command=browse_csv)
btn_csv.grid(row=4, column=1, pady=(0, 10))

# --- Action & Status ---
btn_run = tk.Button(
    frame,
    text="Run Injection",
    command=run_injection,
    bg="#2c3e50",
    fg="white",
    font=("Helvetica", 10, "bold"),
    pady=4,
)
btn_run.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 5))

status_label = tk.Label(frame, text="", font=("Helvetica", 9, "italic"))
status_label.grid(row=6, column=0, columnspan=2, pady=(5, 0))

root.mainloop()