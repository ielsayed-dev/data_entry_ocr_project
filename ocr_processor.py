import os
import re
import cv2
import numpy as np
import pytesseract
import pandas as pd
from pdf2image import convert_from_path
from PIL import Image

# --- CONFIGURATION ---
INPUT_FOLDER = 'input_pdfs'
OUTPUT_FILE = 'final_output_v3.xlsx'

# --- REGEX PATTERNS ---
# Matches Irish postcodes (e.g., V93 A4V5 or V23 RH94)
POSTCODE_PATTERN = r'[A-Z0-9]{3}\s?[A-Z0-9]{4}' 
# Matches leading numbering (e.g., "2763" or "10.")
LEADING_NUMS = r'^[\d\.\-\s]+'
# Matches flags (S, D, E) only if they appear alone at start
FLAGS_PATTERN = r'^[SDE]\s+'
# Header/Footer keywords to ignore
IGNORE_KEYWORDS = ["polling", "district", "electoral", "division", "booth", "no electors", "page", "register", "electors"]

# Global list to temporarily hold name fragments before merging
name_fragment_buffer = [] 

def is_crossed_out(image_path, box):
    """
    Checks if a horizontal line cuts through the text box.
    """
    x, y, w, h = box
    try:
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        crop = img[y:y+h, x:x+w]
        _, thresh = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 1))
        detected_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horizontal_kernel, iterations=2)
        
        if cv2.countNonZero(detected_lines) > 20: 
            return "YES"
    except Exception:
        pass
    return "NO"

def clean_text(text):
    text = re.sub(LEADING_NUMS, '', text)
    text = re.sub(FLAGS_PATTERN, '', text)
    text = re.sub(r'\(\d+\)', '', text)
    text = re.sub(r',', '', text) # NEW: Remove commas which often cause breaks
    return text.strip()

def process_column(boxes, image_path, file_name, page_num):
    """
    Process a sorted list of text boxes for a single column.
    CRITICAL FIXES: Name merging and exclusive address detection.
    """
    column_data = []
    current_address_lines = []
    
    # Sort boxes top-to-bottom (by Y coordinate)
    boxes.sort(key=lambda x: x[1])

    for box in boxes:
        text, y, x, w, h = box[0], box[1], box[2], box[3], box[4]
        clean_val = clean_text(text)
        
        # --- SKIP LOGIC ---
        if len(clean_val) < 2: continue
        if any(keyword in clean_val.lower() for keyword in IGNORE_KEYWORDS): continue
        
        # 1. ADDRESS/POSTCODE DETECTION (HIGH PRIORITY)
        # If we see a postcode, we clear the name buffer and start collecting address lines
        if re.search(POSTCODE_PATTERN, clean_val):
            # Address line, clear name buffer
            name_fragment_buffer.clear()
            current_address_lines.append(clean_val)
            continue
        
        # 2. CONTINUING ADDRESS LINES
        # If we have an active address (lines collected) and the current line is short (not a new name),
        # treat it as part of the current address. This prevents short words being interpreted as names.
        if current_address_lines and len(clean_val) < 20:
             current_address_lines.append(clean_val)
             continue
        
        # 3. NAME FRAGMENT COLLECTION
        # If we reach here, it's a name or a name fragment.
        
        # If we had address lines, finalize the previous record before starting a new name
        if current_address_lines:
            # Check the LAST entry added to column_data, which should be the name that owns this address
            if column_data:
                 column_data[-1]['Address'] = ", ".join(current_address_lines)
            current_address_lines.clear() # Reset address collector
            
        # Add the current text as a fragment
        name_fragment_buffer.append(clean_val)
        
        # MERGE FRAGMENTS: If the buffer has more than one fragment, merge it into one name and clear the buffer
        if len(name_fragment_buffer) == 1:
            # Check Crossed Out Status
            crossed_status = is_crossed_out(image_path, (x, y, w, h))
            
            # This is the FIRST fragment, start a new entry
            new_entry = {
                'File Name': file_name,
                'Page': page_num,
                'Name': clean_val,
                'Address': '',
                'Crossed Out': crossed_status
            }
            column_data.append(new_entry)
            
        elif len(name_fragment_buffer) > 1:
            # This is a subsequent fragment (e.g., "McCarthy" after "Linda"). 
            # Merge the fragments into the LAST entry's Name field.
            merged_name = " ".join(name_fragment_buffer)
            column_data[-1]['Name'] = merged_name
            name_fragment_buffer.clear() # Clear buffer after merging

    # Final cleanup (if the last item was an address)
    if current_address_lines and column_data:
        column_data[-1]['Address'] = ", ".join(current_address_lines)
        
    return column_data

def process_page_layout(image_path, file_name, page_num):
    data = pytesseract.image_to_data(Image.open(image_path), output_type=pytesseract.Output.DICT, config='--psm 11')
    
    img_width = Image.open(image_path).size[0]
    img_height = Image.open(image_path).size[1]
    midpoint = img_width / 2
    
    left_column_boxes = []
    right_column_boxes = []
    
    for i in range(len(data['text'])):
        text = data['text'][i].strip()
        conf = int(data['conf'][i])
        
        if not text or conf < 30: continue

        x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
        
        # HEADER/FOOTER FILTER
        if y < 100 or y > (img_height - 100): continue

        box_data = (text, y, x, w, h)

        # Assign to LEFT or RIGHT column
        if x < midpoint:
            left_column_boxes.append(box_data)
        else:
            right_column_boxes.append(box_data)
            
    # Process Left, THEN Right
    page_records = []
    
    # Global buffer must be cleared for each page
    global name_fragment_buffer 
    name_fragment_buffer = []

    if left_column_boxes:
        page_records.extend(process_column(left_column_boxes, image_path, file_name, page_num))
    
    # Reset buffer between columns
    name_fragment_buffer = [] 
    
    if right_column_boxes:
        page_records.extend(process_column(right_column_boxes, image_path, file_name, page_num))
        
    return page_records

def main():
    all_records = []
    
    if not os.path.exists(INPUT_FOLDER):
        os.makedirs(INPUT_FOLDER)
        print("Please put PDFs in 'input_pdfs' folder.")
        return

    pdf_files = [f for f in os.listdir(INPUT_FOLDER) if f.lower().endswith('.pdf')]
    print(f"Found {len(pdf_files)} PDF files.")

    for pdf_file in pdf_files:
        print(f"Processing {pdf_file}...")
        full_path = os.path.join(INPUT_FOLDER, pdf_file)
        
        try:
            pages = convert_from_path(full_path, dpi=300)
        except Exception as e:
            print(f"Skipping {pdf_file}: {e}")
            continue
            
        for i, page_img in enumerate(pages):
            temp_img_path = "temp_page.jpg"
            page_img.save(temp_img_path, 'JPEG')
            
            page_data = process_page_layout(temp_img_path, pdf_file, i+1)
            
            if page_data: 
                all_records.extend(page_data)
            
            os.remove(temp_img_path)

    if all_records:
        df = pd.DataFrame(all_records)
        df.to_excel(OUTPUT_FILE, index=False)
        print(f"Done! Saved to {OUTPUT_FILE}")
    else:
        print("No names found. Check image quality or Regex patterns.")

if __name__ == "__main__":
    main()