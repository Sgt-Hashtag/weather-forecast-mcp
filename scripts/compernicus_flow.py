import openeo
import pandas as pd
import matplotlib.pyplot as plt
import geopandas as gpd
from fpdf import FPDF
import datetime
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication # Fixed: For PDF
from pathlib import Path
import rasterio
from rasterio.plot import show
import tempfile

# ====================== CONFIG ======================
YOUR_FARM_GEOJSON = {  
    "type": "Polygon",
    "coordinates": [[[77.5, 28.5], [77.6, 28.5], [77.6, 28.6], [77.5, 28.6], [77.5, 28.5]]]
}
LAST_RUN_FILE = "last_processed.txt"
EMAIL_FROM = "your.email@gmail.com"
EMAIL_TO = "recipient@email.com"
EMAIL_PASSWORD = "your-app-password"  # Generate this in Google Account Settings > Security
# ===================================================

def connect_openeo():
    print("Connecting to Copernicus Data Space...")
    con = openeo.connect("openeo.dataspace.copernicus.eu")
    # This will trigger a browser login if no valid token exists
    con.authenticate_oidc()
    return con

def get_latest_ndvi(con, farm_geojson):
    print("Fetching Sentinel-2 data...")
    
    # 1. Calculate dynamic date range (Last 30 days)
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=30)
    
    # Format as strings "YYYY-MM-DD"
    temporal_extent = [start_date.isoformat(), end_date.isoformat()]
    print(f"Requesting data for period: {temporal_extent}")

    # 2. Load Collection with correct date format
    s2 = con.load_collection(
        "SENTINEL2_L2A",
        temporal_extent=temporal_extent,  # <-- Fixed: Use list of date strings
        bands=["B04", "B08", "SCL"],
        max_cloud_cover=50 
    )
    
    # 3. Calculate NDVI
    red = s2.band("B04")
    nir = s2.band("B08")
    ndvi = (nir - red) / (nir + red)
    
    # 4. Cloud Masking (SCL)
    scl = s2.band("SCL")
    # Keep only clear pixels: 4 (Veg), 5 (Non-Veg), 6 (Water), 11 (Snow), 12 (Shadow)
    # Mask out: 0, 1, 2, 3, 7, 8, 9, 10 (Clouds, Shadows, Bad Data)
    good_pixels = (scl == 4) | (scl == 5) | (scl == 6) | (scl == 11) | (scl == 12)
    ndvi_masked = ndvi.mask(good_pixels)
    
    # 5. Get Stats (Mean NDVI)
    stats = ndvi_masked.aggregate_spatial(geometries=farm_geojson, reducer="mean")
    
    # 6. Execute Stats Job
    print("Calculating statistics...")
    try:
        # execute_batch is safer for larger jobs, but execute() is faster for small stats
        stats_result = stats.execute_batch(title="Farm NDVI Stats")
        stats_result.download_file("ndvi_stats.csv")
    except Exception as e:
        print(f"Batch job failed, trying synchronous execution... Error: {e}")
        # Fallback to synchronous execution for small JSON results
        result_json = stats.execute()
        import json
        with open("ndvi_stats.csv", "w") as f:
            # Convert JSON result to CSV format manually if needed
            # Usually result_json is a list of features
            df = pd.json_normalize(result_json)
            df.to_csv("ndvi_stats.csv", index=False)

    # 7. Execute Raster Job (for the map)
    print("Starting raster map job... (This may take 2-5 minutes)")
    raster_job = ndvi_masked.save_result(format="GTiff")
    job_raster = raster_job.create_job(title="Farm NDVI Map")
    
    # Start and wait for completion
    job_raster.start_and_wait() 
    job_raster.get_results().download_file("ndvi_map.tif")
    
    # 8. Process Results
    if not os.path.exists("ndvi_stats.csv"):
        raise FileNotFoundError("Stats CSV not generated.")
        
    df = pd.read_csv("ndvi_stats.csv")
    
    # Debug: Print dataframe to see what we got
    print("Stats DataFrame Head:")
    print(df.head())
    
    if df.empty or 'mean' not in df.columns:
        raise ValueError("No valid NDVI data returned (possibly all clouds or no data in period).")
        
    # Get the last valid mean value (most recent date)
    # Drop NaN values caused by full cloud cover on specific dates
    valid_means = df["mean"].dropna()
    
    if valid_means.empty:
        raise ValueError("All retrieved dates were fully cloudy or had no data.")
        
    latest_ndvi = float(valid_means.iloc[-1])
    print(f"Latest NDVI Value: {latest_ndvi:.3f}")
    
    return latest_ndvi, "ndvi_map.tif"

def classify_stress(ndvi_value, ndvi_map_path):
    print("Classifying stress levels...")
    if ndvi_value > 0.65:
        status = "Healthy"
        deficit = "None"
    elif ndvi_value > 0.45:
        status = "Moderate stress "
        deficit = "Possible water/nutrient deficit (~20-40% yield impact)"
    else:
        status = "High stress "
        deficit = "Urgent irrigation/fertilizer needed (~50%+ yield impact)"
    
    # Generate Zone Map
    try:
        with rasterio.open(ndvi_map_path) as src:
            data = src.read(1)
            # Mask nodata values for cleaner plot
            data = np.ma.masked_where(data == src.nodata, data)
            
            plt.figure(figsize=(8,6))
            show(data, cmap="RdYlGn", title=f"Farm NDVI Zones - {datetime.date.today()}")
            plt.colorbar(label="NDVI (Green=Healthy)")
            plt.savefig("zone_map.png")
            plt.close() # Free memory
    except Exception as e:
        print(f"Error generating map: {e}")
        # Create a dummy image if map fails so PDF doesn't crash
        plt.figure()
        plt.text(0.5, 0.5, "Map Generation Failed", ha='center')
        plt.savefig("zone_map.png")
        plt.close()

    return status, deficit, "zone_map.png"

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

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    import numpy as np # Added missing import
    
    # Check if new data since last run
    last_run_date = None
    if Path(LAST_RUN_FILE).exists():
        try:
            last_run_date = datetime.date.fromisoformat(Path(LAST_RUN_FILE).read_text().strip())
        except ValueError:
            last_run_date = datetime.date(2020, 1, 1)
    else:
        last_run_date = datetime.date(2020, 1, 1)
        
    today = datetime.date.today()
    
    # Simple check: if we ran today, skip. 
    # In production, you might want to check if new Sentinel-2 tiles actually exist.
    if last_run_date >= today:
        print("Script already run today. Skipping.")
    else:
        try:
            con = connect_openeo()
            ndvi_value, ndvi_map = get_latest_ndvi(con, YOUR_FARM_GEOJSON)
            status, deficit, zone_map = classify_stress(ndvi_value, ndvi_map)
            report = create_pdf(ndvi_value, status, deficit, zone_map)
            send_email(report, zone_map)
            
            # Update last run
            Path(LAST_RUN_FILE).write_text(today.isoformat())
            print("Full pipeline complete!")
        except Exception as e:
            print(f"Pipeline failed: {e}")
