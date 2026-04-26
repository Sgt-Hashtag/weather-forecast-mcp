import openeo
import pandas as pd
import matplotlib.pyplot as plt
import geopandas as gpd
from fpdf import FPDF
import datetime
from datetime import timedelta  # Fixed: Import timedelta explicitly
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from pathlib import Path
import rasterio
from rasterio.plot import show
import numpy as np
import matplotlib.colors as mcolors
import traceback

# ====================== CONFIG ======================
YOUR_FARM_GEOJSON = {  
    "type": "Polygon",
     "coordinates": [
        [
            [90.35, 23.75],  # Southwest corner
            [90.45, 23.75],  # Southeast corner
            [90.45, 23.85],  # Northeast corner
            [90.35, 23.85],  # Northwest corner
            [90.35, 23.75]   # Close the loop
        ]
    ]
}
LAST_RUN_FILE = "last_processed.txt"
EMAIL_FROM = "your.email@gmail.com"
EMAIL_TO = "recipient@email.com"
EMAIL_PASSWORD = "your-app-password" 
# ===================================================

def connect_openeo():
    print("Connecting to Copernicus Data Space...")
    con = openeo.connect("openeo.dataspace.copernicus.eu")
    con.authenticate_oidc()
    return con

def get_latest_ndvi(con, farm_geojson):
    print("Fetching Sentinel-2 data for Dhaka...")
    
    end_date = datetime.date.today()
    start_date = end_date - timedelta(days=30)
    temporal_extent = [start_date.isoformat(), end_date.isoformat()]

    # 1. Load Collection
    s2 = con.load_collection(
        "SENTINEL2_L2A",
        spatial_extent=farm_geojson,
        temporal_extent=temporal_extent,
        bands=["B04", "B08", "SCL"],
        max_cloud_cover=50 
    )
    
    # 2. Calculate NDVI
    red = s2.band("B04")
    nir = s2.band("B08")
    ndvi = (nir - red) / (nir + red)
    
    # 3. Cloud Masking
    scl = s2.band("SCL")
    good_pixels = (scl == 4) | (scl == 5) | (scl == 6) | (scl == 11) | (scl == 12)
    ndvi_masked = ndvi.mask(good_pixels)
    
    # 4. FIX: Reduce time dimension to a single image (Median)
    # This prevents the MultipleAssetException by turning 30 days of data into 1 file.
    ndvi_reduced = ndvi_masked.reduce_dimension(dimension="t", reducer="median")

    # 5. Stats (Using original ndvi_masked for statistics)
    stats_json = ndvi_masked.aggregate_spatial(geometries=farm_geojson, reducer="mean").execute()
    df = pd.json_normalize(stats_json)
    df.to_csv("ndvi_stats.csv", index=False)
    
    # 6. Execute Raster Job (Using reduced image)
    print("Starting raster map job...")
    raster_job = ndvi_reduced.save_result(format="GTiff")
    job_raster = raster_job.create_job(title="Dhaka NDVI Map")
    job_raster.start_and_wait() 
    
    # Download
    results = job_raster.get_results()
    results.download_file("ndvi_map.tif")
    
    return float(df["mean"].dropna().iloc[-1]), "ndvi_map.tif"

def create_pdf(ndvi_value, status, deficit, zone_map_path):
    print("Generating PDF report...")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "Farm Sentinel-2 Report", ln=1, align="C") # Removed emoji for font compatibility
    pdf.set_font("Arial", "", 12)
    pdf.cell(0, 10, f"Date: {datetime.date.today()}", ln=1)
    pdf.cell(0, 10, f"Average NDVI: {ndvi_value:.3f}", ln=1)
    pdf.cell(0, 10, f"Status: {status}", ln=1)
    pdf.multi_cell(0, 10, f"Deficit Estimate: {deficit}")
    
    # Add image if it exists
    if os.path.exists(zone_map_path):
        pdf.image(zone_map_path, x=10, y=70, w=180)
    
    pdf_output_path = "farm_report.pdf"
    pdf.output(pdf_output_path)
    return pdf_output_path

def send_email(report_path, zone_map_path):
    print("Sending email...")
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = "New Sentinel-2 Farm Report Ready"
    
    body = "Please find attached your latest farm health report."
    msg.attach(MIMEText(body, "plain"))
    
    # Attach PDF correctly
    with open(report_path, "rb") as f:
        part = MIMEApplication(f.read(), Name="farm_report.pdf")
        part['Content-Disposition'] = 'attachment; filename="farm_report.pdf"'
        msg.attach(part)
        
    # Attach Image
    with open(zone_map_path, "rb") as f:
        img = MIMEImage(f.read(), _subtype="png")
        img.add_header('Content-Disposition', 'attachment', filename="zone_map.png")
        msg.attach(img)
    
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(" Email sent successfully!")
    except Exception as e:
        print(f" Failed to send email: {e}")
        
def classify_and_visualize(ndvi_value, ndvi_map_path):
    """
    Loads the raster, classifies pixels, and opens an interactive window.
    """
    print("Loading raster data for visualization...")
    
    if not os.path.exists(ndvi_map_path):
        raise FileNotFoundError(f"Map file {ndvi_map_path} not found.")

    # 1. Load the GeoTIFF
    with rasterio.open(ndvi_map_path) as src:
        ndvi_data = src.read(1)
        ndvi_data = np.ma.masked_where(ndvi_data == src.nodata, ndvi_data)
        ndvi_data = np.ma.masked_invalid(ndvi_data)

    # 2. Classify each pixel
    classification = np.zeros_like(ndvi_data)
    classification[ndvi_data > 0.45] = 1  # Moderate
    classification[ndvi_data > 0.65] = 2  # Healthy
    
    # Define colors for classification
    cmap_class = mcolors.ListedColormap(['#d73027', '#fdae61', '#1a9850'])
    bounds = [0, 1, 2, 3]
    norm_class = mcolors.BoundaryNorm(bounds, cmap_class.N)

    # Define colors for NDVI continuous map
    cmap_ndvi = plt.cm.RdYlGn

    # 3. Create Interactive Plot
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    
    # --- Left Plot: Continuous NDVI ---
    im1 = axes[0].imshow(ndvi_data, cmap=cmap_ndvi, vmin=0, vmax=1)
    axes[0].set_title(f"Continuous NDVI\nAvg: {ndvi_value:.3f}", fontsize=12)
    axes[0].axis('off')
    plt.colorbar(im1, ax=axes[0], label="NDVI Value")

    # --- Right Plot: Classified Zones ---
    im2 = axes[1].imshow(classification, cmap=cmap_class, norm=norm_class)
    axes[1].set_title("Stress Classification Zones", fontsize=12)
    axes[1].axis('off')
    
    # Custom legend
    labels = ["High Stress (<0.45)", "Moderate (0.45-0.65)", "Healthy (>0.65)"]
    handles = [plt.Rectangle((0,0),1,1, color=cmap_class(i)) for i in range(3)]
    axes[1].legend(handles, labels, loc='upper right', fontsize=8)

    # Status Text
    if ndvi_value > 0.65:
        status_text = "Status: HEALTHY 🌱"
        color = "green"
    elif ndvi_value > 0.45:
        status_text = "Status: MODERATE STRESS ⚠️"
        color = "orange"
    else:
        status_text = "Status: HIGH STRESS ❌"
        color = "red"
        
    fig.suptitle(f"Farm Report - {datetime.date.today()} | {status_text}", 
                 fontsize=14, fontweight='bold', color=color)

    plt.tight_layout()
    plt.show()

def run_pipeline_visual():
    """
    Main execution flow for visualization mode.
    """
    last_run_date = None
    if Path(LAST_RUN_FILE).exists():
        try:
            last_run_date = datetime.date.fromisoformat(Path(LAST_RUN_FILE).read_text().strip())
        except ValueError:
            last_run_date = datetime.date(2020, 1, 1)
    else:
        last_run_date = datetime.date(2020, 1, 1)
        
    today = datetime.date.today()
    
    # Check if we already ran today
    if last_run_date >= today:
        print("Script already run today. Using existing files...")
        if os.path.exists("ndvi_stats.csv") and os.path.exists("ndvi_map.tif"):
            df = pd.read_csv("ndvi_stats.csv")
            valid_means = df["mean"].dropna()
            if not valid_means.empty:
                ndvi_value = float(valid_means.iloc[-1])
                classify_and_visualize(ndvi_value, "ndvi_map.tif")
            else:
                print("Existing data is invalid. Please clear last_processed.txt and retry.")
        else:
            print("No existing files found. Please delete last_processed.txt and retry.")
    else:
        try:
            con = connect_openeo()
            ndvi_value, ndvi_map = get_latest_ndvi(con, YOUR_FARM_GEOJSON)
            
            # Visualize
            classify_and_visualize(ndvi_value, ndvi_map)
            
            # Update last run
            Path(LAST_RUN_FILE).write_text(today.isoformat())
            print("✅ Visualization complete!")
            
        except Exception as e:
            print(f"❌ Pipeline failed: {e}")
            traceback.print_exc()

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    run_pipeline_visual()



