import os
import re
import cv2
import numpy as np
import pytesseract
import pandas as pd
from pdf2image import convert_from_path
from PIL import Image

# --- CONFIGURATION ---
INPUT_FOLDER = 'input_pdfs'   # Create this folder and put your PDFs inside
OUTPUT_FILE = 'final_output.xlsx'

# Regex patterns based on your PDF scope
# Detects postcodes like "V93 A4V5" or similar patterns seen in the file
POSTCODE_PATTERN = r'[A-Z0-9]{3}\s?[A-Z0-9]{4}' 
# Detects leading numbers to ignore (e.g., "2763 O'Keeffe")
LEADING_NUMS = r'^\d+\s*'
# Detects S, D, E flags at the start
FLAGS_PATTERN = r'^[SDE]\s+'

def is_crossed_out(image_crop):
    """
    Uses OpenCV to detect if a horizontal line goes through the text.
    Returns "YES" or "NO".
    """
    # 1. Convert crop to grayscale
    gray = cv2.cvtColor(np.array(image_crop), cv2.COLOR_RGB2GRAY)
    
    # 2. Thresholding to get binary image (black text on white background)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 3. Create a horizontal kernel to isolate horizontal lines
    # The width (20) determines how long the line must be to be detected
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1))
    
    # 4. Morphological operation to extract lines
    detected_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horizontal_kernel, iterations=2)
    
    # 5. Count non-zero pixels (white pixels = lines)
    non_zero_pixels = cv2.countNonZero(detected_lines)
    
    # If we see enough "line" pixels, it's crossed out
    # You may need to tune this threshold value (50) based on real tests
    if non_zero_pixels > 50:
        return "YES"
    return "NO"

def clean_text(text):
    """
    Cleans the name string: removes numbers, flags, and special headers.
    """
    # Remove leading numbers (e.g., "2763")
    text = re.sub(LEADING_NUMS, '', text)
    # Remove flags (e.g., "D ", "S ")
    text = re.sub(FLAGS_PATTERN, '', text)
    # Remove brackets if they contain only numbers (e.g., "(15)")
    text = re.sub(r'\(\d+\)', '', text)
    return text.strip()

def process_page_layout(image_path, file_name, page_num):
    """
    OCR processing for a single page image.
    Separates columns and pairs addresses.
    """
    extracted_data = []
    
    # Get OCR data including bounding boxes (left, top, width, height)
    # config='--psm 6' assumes a sparse text block, good for lists
    data = pytesseract.image_to_data(Image.open(image_path), output_type=pytesseract.Output.DICT, config='--psm 6')
    
    width, height = Image.open(image_path).size
    midpoint = width / 2
    
    n_boxes = len(data['text'])
    
    current_entry = None # Holder for the name currently being processed
    
    # Iterate through every detected text block
    for i in range(n_boxes):
        text = data['text'][i].strip()
        
        # Skip empty confidence or empty text
        if int(data['conf'][i]) < 0 or not text:
            continue
            
        # Coordinates
        x, y, w, h = data['left'][i], data['top'][i], data['width'][i], data['height'][i]
        
        # --- COLUMN LOGIC ---
        # If x < midpoint, it's Column 1. If x > midpoint, it's Column 2.
        # We can treat them as a continuous stream if we process order correctly, 
        # but simpler is to just capture the line.
        
        # --- FILTERING LOGIC ---
        # Ignore headers/footers based on Y position (approximate)
        if y < 50 or y > (height - 50): 
            continue
            
        # Ignore specific keywords from your scope
        if text.lower() in ["polling", "district", "electoral", "division", "booth", "no", "electors"]:
            continue

        # --- ADDRESS DETECTION ---
        # If text looks like a postcode or address part, append to previous name
        if re.search(POSTCODE_PATTERN, text):
            if current_entry:
                current_entry['Address'] += " " + text
            continue
            
        # --- NAME DETECTION ---
        # If it's not an address, assume it's a new name line
        cleaned_name = clean_text(text)
        
        if len(cleaned_name) > 2: # Ignore noise/short artifacts
            
            # CHECK FOR CROSS OUT
            # Crop the original image to just this word/line
            img = Image.open(image_path)
            crop = img.crop((x, y, x+w, y+h))
            is_crossed = is_crossed_out(crop)
            
            # Add to list
            entry = {
                'File Name': file_name,
                'Page': page_num,
                'Name': cleaned_name,
                'Address': '', # Will be filled if next lines are address
                'Crossed Out': is_crossed
            }
            extracted_data.append(entry)
            current_entry = entry
            
    return extracted_data

def main():
    all_records = []
    
    # 1. Setup Input Directory
    if not os.path.exists(INPUT_FOLDER):
        os.makedirs(INPUT_FOLDER)
        print(f"Created folder '{INPUT_FOLDER}'. Please put your PDFs there.")
        return

    pdf_files = [f for f in os.listdir(INPUT_FOLDER) if f.lower().endswith('.pdf')]
    print(f"Found {len(pdf_files)} PDF files.")

    # 2. Iterate through files
    for pdf_file in pdf_files:
        print(f"Processing {pdf_file}...")
        full_path = os.path.join(INPUT_FOLDER, pdf_file)
        
        # Convert PDF to images (one per page)
        try:
            pages = convert_from_path(full_path, dpi=300) # 300 DPI for better OCR
        except Exception as e:
            print(f"Error converting {pdf_file}: {e}")
            continue
            
        # Process each page
        for i, page_img in enumerate(pages):
            temp_img_path = "temp_page.jpg"
            page_img.save(temp_img_path, 'JPEG')
            
            page_data = process_page_layout(temp_img_path, pdf_file, i+1)
            all_records.extend(page_data)
            
            # Clean up temp file
            os.remove(temp_img_path)

    # 3. Save to Excel
    if all_records:
        df = pd.DataFrame(all_records)
        df.to_excel(OUTPUT_FILE, index=False)
        print(f"\nSUCCESS! Processed {len(all_records)} names.")
        print(f"Output saved to: {OUTPUT_FILE}")
    else:
        print("No data extracted. Check your PDF folder or OCR settings.")

if __name__ == "__main__":
    main()
