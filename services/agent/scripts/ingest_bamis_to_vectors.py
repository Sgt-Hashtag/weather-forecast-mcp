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
def _cell_text(c) -> str:
    """Return clean text from a cell, or empty string for None/blank cells."""
    if c is None: return ""
    s = " ".join(str(c).split()).strip()  # collapse newlines/whitespace
    return "" if s in ("", "None") else clean_bengali(s)


def _is_stage_text(s: str) -> bool:
    """True if the string looks like a crop stage name (not a bare number)."""
    if not s: return False
    # Reject if the string is purely numeric (e.g. mean-temp or RH values)
    cleaned = s.replace('.', '').replace('-', '').replace(' ', '')
    return not cleaned.isdigit()


def _assign_stages_from_rows(stage_rows: List[List], week_map: dict):
    """
    Collect stage text from one or more continuation rows, then assign each
    stage to the week whose column index is the closest match on the left.

    Stage text in BAMIS PDFs appears at the *start column* of a span, not at
    every individual week column, so we forward-fill once we have all text.
    """
    # col_idx → list of text fragments (multi-row cells get concatenated)
    col_text: Dict[int, List[str]] = {}
    for row in stage_rows:
        for col_i, c in enumerate(row):
            t = _cell_text(c)
            if t and _is_stage_text(t):
                col_text.setdefault(col_i, []).append(t)

    if not col_text:
        return

    # Build sorted list of (actual_col_index, text) for stage anchors
    stage_anchors = sorted(
        (col_i, " ".join(parts)) for col_i, parts in col_text.items()
    )

    # For each week, find the rightmost stage anchor whose col ≤ week's actual col
    # week col_idx is j in row[1:], so actual col = col_idx + 1
    weeks_sorted = sorted(week_map.values(), key=lambda w: w["col_idx"])
    anchor_idx = 0
    current_stage = ""
    for wk_data in weeks_sorted:
        actual_col = wk_data["col_idx"] + 1
        # Advance anchor pointer while next anchor is still ≤ this week's col
        while anchor_idx + 1 < len(stage_anchors) and stage_anchors[anchor_idx + 1][0] <= actual_col:
            anchor_idx += 1
        if stage_anchors and stage_anchors[anchor_idx][0] <= actual_col:
            current_stage = stage_anchors[anchor_idx][1]
        wk_data["crop_stage"] = current_stage


def parse_bamis_pdf(pdf_path: Path, crop: str, region: str) -> CropCalendar:
    """Parses a transposed BAMIS calendar PDF into Pydantic models."""
    calendar = CropCalendar(crop=crop, region=region)
    week_map = {}

    try:
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[0]
            table = page.extract_table()
            if not table: return calendar

            # Find Header Row (Contains Std.Week)
            header_idx = -1
            for i, row in enumerate(table):
                digit_count = sum(1 for c in row if clean_bengali(str(c)).replace('.','').isdigit())
                if digit_count > 5:
                    header_idx = i
                    for j, c in enumerate(row[1:]):
                        val = clean_bengali(str(c))
                        if val.replace('.','').isdigit():
                            wk_num = int(float(val))
                            week_map[wk_num] = {"col_idx": j, "week_number": wk_num, "month": "", "crop_stage": ""}
                    break

            # Fallback: search for "Std.Week"
            if header_idx == -1:
                for i, row in enumerate(table):
                    if row and any("std.week" in str(c).lower() for c in row if c):
                        header_idx = i
                        for j, c in enumerate(row[1:]):
                            val = clean_bengali(str(c))
                            if val.replace('.','').isdigit() or '-' in val:
                                try:
                                    wk_num = int(val.split('-')[0])
                                    week_map[wk_num] = {"col_idx": j, "week_number": wk_num, "month": "", "crop_stage": ""}
                                except: pass
                        break

            if header_idx == -1: return calendar

            # --- Month row: scan the FULL table (may appear before header row) ---
            for row in table:
                if not row: continue
                key = _cell_text(row[0]).lower()
                if "month" in key:
                    month_anchors = sorted(
                        (j, _cell_text(c))
                        for j, c in enumerate(row[1:])
                        if _cell_text(c)
                    )
                    if month_anchors:
                        weeks_sorted = sorted(week_map.values(), key=lambda w: w["col_idx"])
                        anchor_idx = 0
                        current_month = ""
                        for wk_data in weeks_sorted:
                            while (anchor_idx + 1 < len(month_anchors) and
                                   month_anchors[anchor_idx + 1][0] <= wk_data["col_idx"]):
                                anchor_idx += 1
                            if month_anchors and month_anchors[anchor_idx][0] <= wk_data["col_idx"]:
                                current_month = month_anchors[anchor_idx][1]
                            wk_data["month"] = current_month
                    break

            # Collect stage continuation rows (rows with no key but stage-like text)
            in_stage_section = False
            stage_rows: List[List] = []
            # Fallback: keyless rows with content that may contain stage text
            keyless_content_rows: List[List] = []
            past_numeric_section = False

            for row in table[header_idx + 1:]:
                if not row: continue

                key = _cell_text(row[0]).lower()

                # --- Climate Parameters ---
                param_key = None
                if "rainfall" in key and "warning" not in key: param_key = "rainfall_mm"
                elif "max. temp" in key: param_key = "max_temp_c"
                elif "min. temp" in key: param_key = "min_temp_c"
                elif "rhmax" in key: param_key = "rh_max_percent"
                elif "rhmin" in key: param_key = "rh_min_percent"

                if param_key:
                    in_stage_section = False
                    past_numeric_section = True
                    for wk_num, wk_data in week_map.items():
                        val_cell = row[wk_data["col_idx"] + 1]
                        wk_data[param_key] = extract_numeric(str(val_cell))

                # --- Stage: mark section start, collect continuation rows ---
                # "Stages" label may appear in row[0] or another cell in the row
                elif "stage" in key or any("stage" in _cell_text(c).lower() for c in row if c):
                    in_stage_section = True
                    stage_rows = [row]  # label row may itself have some text

                elif in_stage_section:
                    # Continuation of stage section (no key, or empty key)
                    if not key:
                        stage_rows.append(row)
                    else:
                        # A new named section starts — finalize stages and stop
                        _assign_stages_from_rows(stage_rows, week_map)
                        in_stage_section = False
                        stage_rows = []

                # --- Text-Based Advisories ---
                elif any(k in key for k in ["white fly", "borer", "miner", "blight", "wilting",
                                             "warning", "cloudy", "drought", "hailstorm",
                                             "favorable", "congenial"]):

                    if "warning" in key or "cloudy" in key or "drought" in key or "hail" in key:
                        category = "Weather Warning"
                    elif "favorable" in key or "congenial" in key:
                        category = "Favorable Condition"
                    else:
                        category = "Pest/Disease"

                    name = key.replace("weather warning", "").replace("congenial weather condition for pests& diseases", "").strip().title()

                    # Only include cells that genuinely have text (not None / blank)
                    desc_parts = [_cell_text(c) for c in row[1:] if _cell_text(c)]
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

                # --- Fallback stage collection: keyless rows with non-numeric content ---
                elif not key and past_numeric_section and not in_stage_section:
                    row_has_stage_text = any(_is_stage_text(_cell_text(c)) for c in row)
                    if row_has_stage_text:
                        keyless_content_rows.append(row)

            # Apply any remaining stage rows that weren't closed by a new section
            if in_stage_section and stage_rows:
                _assign_stages_from_rows(stage_rows, week_map)

            # Fallback: if no stages were assigned via label, use keyless content rows
            if all(not wd["crop_stage"] for wd in week_map.values()) and keyless_content_rows:
                _assign_stages_from_rows(keyless_content_rows, week_map)

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