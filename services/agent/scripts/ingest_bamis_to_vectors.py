import pdfplumber
import re
import json
import uuid
from pathlib import Path
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

# ==========================
# BENGALI TO ENGLISH CLEANER
# ==========================
BN_DIGITS = "০১২৩৪৫৬৭৮৯"
EN_DIGITS = "0123456789"
BN_TO_EN_TRANS = str.maketrans(BN_DIGITS, EN_DIGITS)

def clean_bengali(text: str) -> str:
    """Translate Bengali digits to English and strip whitespace."""
    if not text: return ""
    return text.translate(BN_TO_EN_TRANS).strip()

def extract_numeric(val: str) -> Optional[float]:
    """Extract first valid float/int from string (handles '≥20', '20-30', '২৫.৬')."""
    if not val: return None
    cleaned = clean_bengali(val)
    match = re.search(r"[-+]?\d*\.\d+|\d+", cleaned)
    return float(match.group()) if match else None

# ==========================
# PYDANTIC MODELS
# ==========================
class WeeklyClimate(BaseModel):
    crop: str
    region: str
    week_number: int
    month: str
    crop_stage: str
    rainfall_mm: Optional[float] = None
    max_temp_c: Optional[float] = None
    min_temp_c: Optional[float] = None
    rh_max_percent: Optional[float] = None
    rh_min_percent: Optional[float] = None

class AdvisoryCondition(BaseModel):
    crop: str
    region: str
    category: str = Field(..., description="Pest, Disease, Weather Warning, or Favorable")
    name: str
    description: str
    applicable_period: str
    raw_text: str

class VectorChunk(BaseModel):
    id: str
    text: str
    metadata: Dict[str, Any]

class CropCalendar(BaseModel):
    crop: str
    region: str
    weekly_climate: List[WeeklyClimate] = []
    advisories: List[AdvisoryCondition] = []

    def to_vector_chunks(self) -> List[VectorChunk]:
        """Flattens structured data into embedding-ready chunks."""
        chunks = []
        
        # Weekly Climate Chunks
        for week in self.weekly_climate:
            text = (
                f"Crop: {week.crop} | Region: {week.region} | "
                f"Week {week.week_number} ({week.month}) | Stage: {week.crop_stage}. "
                f"Climate: Rainfall {week.rainfall_mm or 'N/A'}mm, "
                f"Temp {week.min_temp_c or '?'}-{week.max_temp_c or '?'}°C, "
                f"RH {week.rh_min_percent or '?'}-{week.rh_max_percent or '?'}%."
            )
            chunks.append(VectorChunk(
                id=f"climate_{week.crop}_{week.region}_w{week.week_number}",
                text=text,
                metadata=week.model_dump(exclude_none=True)
            ))

        # Advisory/Warning Chunks
        for adv in self.advisories:
            text = (
                f"{adv.category} Advisory for {adv.crop} in {adv.region}: "
                f"{adv.name} | {adv.description} | Applies to: {adv.applicable_period}."
            )
            chunks.append(VectorChunk(
                id=f"advisory_{uuid.uuid4().hex[:8]}",
                text=text,
                metadata=adv.model_dump(exclude_none=True)
            ))
            
        return chunks

# ==========================
# PDF EXTRACTION ENGINE
# ==========================
def parse_bamis_pdf(pdf_path: Path, crop: str, region: str) -> CropCalendar:
    """Parses a transposed BAMIS calendar PDF into Pydantic models."""
    calendar = CropCalendar(crop=crop, region=region)
    week_map = {}  # Temporary storage: { week_num: {param: value} }
    week_indices = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Assume main table is on page 1
            page = pdf.pages[0]
            table = page.extract_table()
            if not table: return calendar

            # Find Header Row (Contains Std.Week)
            header_idx = -1
            for i, row in enumerate(table):
                # Check if row contains week numbers
                # We look for rows with many digits
                digit_count = sum(1 for c in row if clean_bengali(str(c)).replace('.','').isdigit())
                if digit_count > 5: 
                    header_idx = i
                    # Extract week numbers from columns 1 onwards (index 0 is key)
                    for j, c in enumerate(row[1:]):
                        val = clean_bengali(str(c))
                        if val.replace('.','').isdigit():
                            # Store column index for later mapping
                            # Use the integer value as key
                            wk_num = int(float(val))
                            week_map[wk_num] = {"col_idx": j, "week_number": wk_num, "month": "", "crop_stage": ""}
                    break
            
            # Fallback if strict header not found: search for "Std.Week"
            if header_idx == -1:
                for i, row in enumerate(table):
                    if row and any("std.week" in str(c).lower() for c in row if c):
                        header_idx = i
                        for j, c in enumerate(row[1:]):
                            val = clean_bengali(str(c))
                            if val.replace('.','').isdigit() or '-' in val:
                                try:
                                    # Handle "49-50" ranges by taking first num
                                    wk_num = int(val.split('-')[0])
                                    week_map[wk_num] = {"col_idx": j, "week_number": wk_num, "month": "", "crop_stage": ""}
                                except: pass
                        break

            if header_idx == -1: return calendar

            # Parse Rows below header
            for row in table[header_idx + 1:]:
                if not row or not row[0]: continue
                key = clean_bengali(str(row[0])).lower()

                # --- Climate Parameters ---
                param_key = None
                if "rainfall" in key and "warning" not in key: param_key = "rainfall_mm"
                elif "max. temp" in key: param_key = "max_temp_c"
                elif "min. temp" in key: param_key = "min_temp_c"
                elif "rhmax" in key: param_key = "rh_max_percent"
                elif "rhmin" in key: param_key = "rh_min_percent"

                if param_key:
                    for wk_num, wk_data in week_map.items():
                        val_cell = row[wk_data["col_idx"] + 1] # +1 because row[0] is key
                        wk_data[param_key] = extract_numeric(str(val_cell))

                # --- Month & Stage Mapping ---
                elif "month" in key:
                    for wk_num, wk_data in week_map.items():
                        wk_data["month"] = clean_bengali(str(row[wk_data["col_idx"] + 1]))
                elif "stage" in key:
                    for wk_num, wk_data in week_map.items():
                        stage_val = clean_bengali(str(row[wk_data["col_idx"] + 1]))
                        if stage_val: wk_data["crop_stage"] = stage_val

                # --- Text-Based Advisories ---
                elif any(k in key for k in ["white fly", "borer", "miner", "blight", "wilting", 
                                             "warning", "cloudy", "drought", "hailstorm", 
                                             "favorable", "congenial"]):
                    
                    # Determine category
                    if "warning" in key or "cloudy" in key or "drought" in key or "hail" in key:
                        category = "Weather Warning"
                    elif "favorable" in key or "congenial" in key:
                        category = "Favorable Condition"
                    else:
                        category = "Pest/Disease"

                    name = key.replace("weather warning", "").replace("congenial weather condition for pests& diseases", "").strip().title()
                    
                    # Join all non-empty cells in this row
                    desc_parts = [clean_bengali(str(c)) for c in row[1:] if clean_bengali(str(c))]
                    full_desc = " | ".join(desc_parts)
                    
                    if full_desc and len(full_desc) > 15:
                        calendar.advisories.append(AdvisoryCondition(
                            crop=crop,
                            region=region,
                            name=name, 
                            category=category, 
                            description=full_desc,
                            applicable_period="General / Spans multiple weeks", 
                            raw_text=full_desc
                        ))

            # Convert dict to Pydantic WeeklyClimate objects
            for wk_num in sorted(week_map.keys()):
                data = week_map[wk_num]
                calendar.weekly_climate.append(WeeklyClimate(
                    crop=crop, 
                    region=region, 
                    **{k: v for k, v in data.items() if k != "col_idx"}
                ))

    except Exception as e:
        print(f" XXXX Error processing {pdf_path.name}: {e}")

    return calendar

# ==========================
# 4. MAIN EXECUTION & EXPORT
# ==========================
def main():
    input_dir = Path("../mcp_weather/data/agri_data/raw")
    output_jsonl = Path("../mcp_weather/data/agri_data/vector_ready_chunks.jsonl")
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    print(f"$$$ Starting BAMIS PDF to Vector Chunk Pipeline...")
    
    # Find all PDFs recursively
    pdf_files = sorted(input_dir.rglob("*.pdf"))
    all_chunks = []

    for pdf_path in pdf_files:
        # Extract crop and region from filename: {crop}_{region}.pdf
        stem = pdf_path.stem
        # Split from the right to handle crops with underscores (e.g., green_gram_kharif_barisal)
        parts = stem.rsplit('_', 1)
        
        if len(parts) == 2:
            crop_name, region_name = parts
        else:
            print(f"### Skipping invalid filename format: {pdf_path.name}")
            continue

        print(f"??? Parsing: {crop_name} | {region_name}")
        
        try:
            calendar = parse_bamis_pdf(pdf_path, crop_name, region_name)
            chunks = calendar.to_vector_chunks()
            all_chunks.extend(chunks)
        except Exception as e:
            print(f"   XXXX Failed: {e}")

    # Save as JSONL
    with open(output_jsonl, "w", encoding="utf-8") as f:
        for chunk in all_chunks:
            f.write(json.dumps(chunk.model_dump(), ensure_ascii=False) + "\n")

    print(f"\n>>> Successfully generated {len(all_chunks)} vector-ready chunks.")
    print(f"$$$ Saved to: {output_jsonl}")

if __name__ == "__main__":
    main()