import logging
from pathlib import Path
from typing import Dict, List
import cv2
import numpy as np
import pytesseract
from bot.services.extract_passport_data import extract_fields, normalize_text

logger = logging.getLogger(__name__)

TESS_LANG = "rus+eng"
TESS_CONFIG = "--oem 3 --psm 6"  # строчный текст

# ---------- базовая предобработка ----------
def _enhance(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 3)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    eq = clahe.apply(gray)
    norm = cv2.normalize(eq, None, 0, 255, cv2.NORM_MINMAX)
    return norm

# ---------- поиск текстовых областей ----------
def _detect_text_regions(gray: np.ndarray) -> List[np.ndarray]:
    """Возвращает список вырезанных регионов с текстом, безопасно по границам."""
    bw = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
    dil = cv2.dilate(bw, kernel, iterations=2)

    cnts, _ = cv2.findContours(dil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = gray.shape
    rois = []

    rects = []
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        # фильтруем шум
        if ch < 20 or cw < 100 or cw / ch < 2:
            continue
        # безопасно обрезаем
        x1 = max(0, x - 5)
        y1 = max(0, y - 3)
        x2 = min(w, x + cw + 5)
        y2 = min(h, y + ch + 3)
        rects.append((y1, x1, x2, y2))

    # сортируем сверху вниз
    rects.sort(key=lambda r: r[0])

    for (y1, x1, x2, y2) in rects:
        roi = gray[y1:y2, x1:x2]
        if roi.size > 0:
            rois.append(roi)
    return rois

# ---------- OCR одного региона ----------
def _ocr(img: np.ndarray) -> str:
    return pytesseract.image_to_string(img, lang=TESS_LANG, config=TESS_CONFIG)

# ---------- публичные функции ----------
def preprocess_image(image_path: Path) -> Path:
    """Попробовать выровнять перспективу, но только если документ действительно найден."""
    img = cv2.imread(str(image_path))
    if img is None:
        return image_path

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(blur)

    thr = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    edges = cv2.Canny(thr, 50, 150)

    cnts, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)
    doc_contour = None
    h, w = gray.shape
    image_area = h * w

    for c in cnts:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        area = cv2.contourArea(c)
        # контур должен быть почти прямоугольным и достаточно большим
        if len(approx) == 4 and area > 0.4 * image_area:
            doc_contour = approx
            break

    if doc_contour is not None:
        pts = doc_contour.reshape(4, 2)
        rect = np.zeros((4, 2), dtype="float32")
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]
        diff = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(diff)]
        rect[3] = pts[np.argmax(diff)]

        (tl, tr, br, bl) = rect
        widthA = np.hypot(br[0] - bl[0], br[1] - bl[1])
        widthB = np.hypot(tr[0] - tl[0], tr[1] - tl[1])
        heightA = np.hypot(tr[0] - br[0], tr[1] - br[1])
        heightB = np.hypot(tl[0] - bl[0], tl[1] - bl[1])
        maxW = int(max(widthA, widthB))
        maxH = int(max(heightA, heightB))

        if maxW > 0 and maxH > 0:
            dst = np.array([[0, 0], [maxW - 1, 0],
                            [maxW - 1, maxH - 1], [0, maxH - 1]], dtype="float32")
            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(img, M, (maxW, maxH))
            gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        else:
            # что-то пошло не так — используем исходное
            gray = enhanced
    else:
        logger.warning("Document contour not found — skipping perspective correction.")
        gray = enhanced

    # лёгкая резкость
    kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
    sharp = cv2.filter2D(gray, -1, kernel)

    out = image_path.parent / f"clean_{image_path.name}"
    cv2.imwrite(str(out), sharp)
    return out

async def process_passport_image(image_path: Path) -> Dict[str, str]:
    """OCR с выделением ROI после коррекции перспективы."""
    img = cv2.imread(str(image_path))
    if img is None:
        return {}

    gray = _enhance(img)
    regions = _detect_text_regions(gray)
    logger.info(f"Found {len(regions)} potential text blocks")

    texts = []
    for idx, roi in enumerate(regions):
        txt = _ocr(roi)
        txt = txt.strip()
        if txt:
            texts.append(txt)
            logger.info(f"Block {idx}: {txt[:80]}")

    if not texts:
        logger.warning("No text recognized.")
        return {}

    raw_text = "\n".join(reversed(texts))
    text = normalize_text(raw_text)

    logger.info(f"\n--- OCR (ROI mode) {image_path.name} ---\n{text}\n------------------------------")

    fields = extract_fields(text)
    fields["raw_text"] = text
    fields["engine"] = "tesseract-roi"
    fields["regions"] = len(regions)
    return fields
