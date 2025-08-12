import logging
import os
import re
import shutil
import io
from itertools import product
from typing import Any, Dict, List, Union, Tuple

import rapidfuzz.fuzz as fuzz
from bs4 import BeautifulSoup, Tag

from desktop_env.evaluators.metrics.utils import are_lists_equal, compare_urls

logger = logging.getLogger("desktopenv.metrics.chrome")


def is_expected_active_tab(active_tab_info: Dict[str, str], rule: Dict[str, Any]) -> Tuple[float, str]:
    """
    Checks if the expected active tab is open in Chrome.
    """
    if not active_tab_info:
        return 0., "No active tab information available"

    match_type = rule['type']

    if match_type == "url":
        expected_url = rule['url']
        if isinstance(active_tab_info, Dict):
            actual_url = active_tab_info.get('url', None)
        else:
            actual_url = active_tab_info
        print("expected_url: {}".format(expected_url))
        print("actual_url: {}".format(actual_url))
        if compare_urls(expected_url, actual_url):
            return 1., f"Active tab URL matches expected: {expected_url}"
        else:
            return 0., f"Active tab URL mismatch - Expected: {expected_url}, Actual: {actual_url}"
    else:
        logger.error(f"Unknown type: {match_type}")
        return 0., f"Unknown match type: {match_type}"


def is_expected_active_tab_approximate(active_tab_info: Dict[str, str], rule: Dict[str, Any]) -> Tuple[float, str]:
    """
    Checks if the expected active tab is open in Chrome, ignoring query parameters in the URL.
    """
    if not active_tab_info:
        return 0., "No active tab information available"

    match_type = rule['type']

    if match_type == "url":
        expected_url = rule['url']
        if isinstance(active_tab_info, Dict):
            actual_url = active_tab_info.get('url', None)
        else:
            actual_url = active_tab_info
        from urllib.parse import urlparse, urlunparse
        def strip_query(url):
            parsed = urlparse(url)
            return urlunparse(parsed._replace(query=""))
        expected_stripped = strip_query(expected_url)
        actual_stripped = strip_query(actual_url)
        if expected_stripped == actual_stripped:
            return 1., f"Active tab URL matches expected (ignoring query params): {expected_stripped}"
        else:
            return 0., f"Active tab URL mismatch - Expected: {expected_stripped}, Actual: {actual_stripped}"
    else:
        logger.error(f"Unknown type: {match_type}")
        return 0., f"Unknown match type: {match_type}"


# rules[expected] is a string-formatted regex
def is_expected_url_pattern_match(result, rules) -> Tuple[float, str]:
    """
    This function is used to search the expected pattern in the url using regex.
    result is the return value of function "activte_tab_info" or return value of function "get_active_url_from_accessTree"   
    """
    if not result:
        return 0., "No result provided for URL pattern matching"

    if type(result) == dict:
        result_url = result["url"]
        print("result url: {}".format(result_url))
    else:
        result_url = result
    # expect_regex = re.compile(rules["expected"])
    patterns = rules["expected"]
    print("expected_regex: {}".format(patterns))
    for pattern in patterns:
        match = re.search(pattern, result_url)
        print(match)
        if not match:
            return 0., f"URL pattern '{pattern}' not found in URL: {result_url}"
    return 1., f"All URL patterns matched in: {result_url}"


def is_expected_installed_extensions(installed_extensions, expected) -> Tuple[float, str]:
    print("installed_extensions: ")
    print(installed_extensions)
    expected_extensions = expected["expected"]

    # whether the expected extensions are installed
    set_expected_extensions = set(expected_extensions)
    set_installed_extensions = set(installed_extensions)

    if set_expected_extensions.issubset(set_installed_extensions):
        return 1., f"All expected extensions are installed: {expected_extensions}"
    else:
        missing = set_expected_extensions - set_installed_extensions
        return 0., f"Missing extensions: {missing}"


def is_expected_tabs(open_tabs: List[Dict[str, str]], rule: Dict[str, Any]) -> Tuple[float, str]:
    """
    Checks if the expected tabs are open in Chrome.
    """

    match_type = rule['type']

    if match_type == "url":
        expected_urls = rule['urls']
        actual_urls = [tab['url'] for tab in open_tabs]
        if not are_lists_equal(expected_urls, actual_urls, compare_urls):
            logger.error("list not match") 
            logger.error(expected_urls)
            logger.error(actual_urls)
            return 0., f"Tab URLs don't match - Expected: {expected_urls}, Actual: {actual_urls}"
        return 1., f"All expected tabs are open with correct URLs"
    else:
        logger.error(f"Unknown type: {match_type}")
        return 0., f"Unknown match type: {match_type}"


def is_expected_bookmarks(bookmarks: List[str], rule: Dict[str, Any]) -> Tuple[float, str]:
    """
    Checks if the expected bookmarks are in Chrome.
    """
    if not bookmarks:
        return 0., "No bookmarks found"
    elif rule['type'] == "bookmark_bar_folders_names":
        bookmark_bar_folders_names = [bookmark['name'] for bookmark in bookmarks['bookmark_bar']['children'] if
                                      bookmark['type'] == 'folder']
        if set(bookmark_bar_folders_names) == set(rule['names']):
            return 1., f"Bookmark bar folders match expected: {rule['names']}"
        else:
            return 0., f"Bookmark bar folders mismatch - Expected: {rule['names']}, Actual: {bookmark_bar_folders_names}"
    elif rule['type'] == "bookmark_bar_websites_urls":
        bookmark_bar_websites_urls = [bookmark['url'] for bookmark in bookmarks['bookmark_bar']['children'] if
                                      bookmark['type'] == 'url']
        if set(bookmark_bar_websites_urls) == set(rule['urls']):
            return 1., f"Bookmark bar URLs match expected"
        else:
            return 0., f"Bookmark bar URLs mismatch - Expected: {rule['urls']}, Actual: {bookmark_bar_websites_urls}"
    elif rule['type'] == "liked_authors_websites_urls":
        # Check if "liked authors" folder exists
        liked_authors_folder = next((bookmark for bookmark in bookmarks['bookmark_bar']['children'] if
                                     bookmark['type'] == 'folder' and bookmark['name'] == 'Liked Authors'), None)
        if liked_authors_folder:
            # Check if it contains the specified URLs
            liked_authors_urls = [bookmark['url'] for bookmark in liked_authors_folder['children'] if
                                  bookmark['type'] == 'url']

            urls = rule['urls']

            for idx, url in enumerate(urls):
                if isinstance(url, str):
                    urls[idx] = [url]

            combinations = product(*urls)

            for combination in combinations:
                if set(combination) == set(liked_authors_urls):
                    return 1., f"Liked Authors folder contains expected URLs"
            return 0., f"Liked Authors folder URLs mismatch - Actual: {liked_authors_urls}"
        else:
            return 0., "Liked Authors folder not found in bookmarks"
    else:
        raise TypeError(f"{rule['type']} not support yet!")


def is_expected_search_query(active_tab_info: Dict[str, str], rules: Dict[str, Any]) -> Tuple[float, str]:
    expected = rules['expect']
    pattern = expected['pattern']
    matched = re.search(pattern, active_tab_info['url'])
    if matched:
        return 1., f"Search query pattern '{pattern}' found in URL"
    return 0., f"Search query pattern '{pattern}' not found in URL: {active_tab_info['url']}"


def compare_pdfs(pdf1_path: Union[str, List[str]], pdf2_path: Union[str, List[str]]) -> Tuple[float, str]:
    """
    Compare two PDF files.
    """
    if type(pdf2_path) != list:
        pdf1_path, pdf2_path = [pdf1_path], [pdf2_path]

    def extract_text_from_pdf(pdf_path):
        """Extract text from each page of the PDF."""
        text = ""
        with fitz.open(pdf_path) as pdf:
            for page in pdf:
                text += page.get_text()
        return text.strip()

    score = 0.
    reasons = []
    for path1, path2 in zip(pdf1_path, pdf2_path):
        try:
            text1 = extract_text_from_pdf(path1)
            text2 = extract_text_from_pdf(path2)
            similarity = fuzz.ratio(text1, text2) / 100
            score += similarity
            reasons.append(f"PDF similarity: {similarity:.2%}")
        except Exception as e:
            logger.info(f"[ERROR]: unexpected error occurred when comparing PDF files: {e}")
            reasons.append(f"Error comparing PDFs: {e}")
    
    avg_score = score / len(pdf2_path)
    return avg_score, f"Average PDF similarity: {avg_score:.2%} - Details: {'; '.join(reasons)}"


import fitz
from PIL import Image
from borb.pdf import Document
from borb.pdf import PDF
import imagehash

from pathlib import Path
import typing


def compare_pdf_images(pdf1_path: str, pdf2_path: str, **kwargs) -> Tuple[float, str]:
    if not pdf1_path or not pdf2_path:
        return 0., "Missing PDF path(s) for comparison"
    if not all(map(os.path.exists, [pdf1_path, pdf2_path])):
        logger.warning(f"PDF file does not exist: {pdf1_path} or {pdf2_path}")
        return 0., f"PDF file does not exist: {pdf1_path} or {pdf2_path}"

    def extract_images_from_pdf(pdf_path):
        pdf_document = fitz.open(pdf_path)
        images = []

        for page_number in range(pdf_document.page_count):
            page = pdf_document[page_number]
            for img_index, img in enumerate(page.get_images(full=True)):
                xref = img[0]
                base_image = pdf_document.extract_image(xref)
                image_bytes = base_image["image"]
                
                # convert to PIL Image
                try:
                    pil_image = Image.open(io.BytesIO(image_bytes))
                    images.append(pil_image)
                except Exception as e:
                    logger.error(f"Failed to process image in {pdf_path} on page {page_number}: {e}")

        return images
    
    temp_dir = Path(pdf1_path).parent / "temp_pdf_comparison"
    os.makedirs(temp_dir, exist_ok=True)
    
    temp_pdf1 = temp_dir / Path(pdf1_path).name
    temp_pdf2 = temp_dir / Path(pdf2_path).name

    shutil.copy(pdf1_path, temp_pdf1)
    shutil.copy(pdf2_path, temp_pdf2)

    try:
        images1 = extract_images_from_pdf(str(temp_pdf1))
        images2 = extract_images_from_pdf(str(temp_pdf2))
    except Exception as e:
        logger.error(f"Error extracting images from PDFs: {e}")
        shutil.rmtree(temp_dir)
        return 0., f"Error extracting images from PDFs: {e}"
    finally:
        shutil.rmtree(temp_dir)


    if len(images1) != len(images2):
        logger.info(f"Different number of images found. Gold: {len(images1)}, Pred: {len(images2)}")
        return 0., f"Different number of images - Expected: {len(images1)}, Actual: {len(images2)}"

    if not images1:
        logger.info("No images found in either PDF. Considering it a match.")
        return 1.0, "No images found in either PDF - considered a match"

    hash_threshold = 5 
    total_score = 0
    mismatched_images = []
    for i, (img1, img2) in enumerate(zip(images1, images2)):
        hash1 = imagehash.phash(img1)
        hash2 = imagehash.phash(img2)
        hash_diff = hash1 - hash2
        
        logger.info(f"Image {i+1}: Gold hash: {hash1}, Pred hash: {hash2}, Hash difference: {hash_diff}")

        if hash_diff <= hash_threshold:
            total_score +=1
        else:
            mismatched_images.append(i+1)
    
    score = total_score / len(images1)
    if score == 1.0:
        return score, f"All {len(images1)} images match"
    else:
        return score, f"Image match score: {score:.2%} - Mismatched images: {mismatched_images}"


def compare_archive(pred_path: str, gold_path: str, **kwargs) -> Tuple[float, str]:
    """
    Compare two archives. Note that the files in the archives should be of the same type.
    """
    file_path = kwargs.pop('file_path', '')

    if not pred_path:
        return 0., "No prediction path provided"
    pred_folder = os.path.splitext(pred_path)[0] + '_pred'
    gold_folder = os.path.splitext(gold_path)[0] + '_gold'

    if os.path.exists(pred_folder):  # remove existing folder for new predictions
        shutil.rmtree(pred_folder, ignore_errors=True)
    os.makedirs(pred_folder)
    shutil.unpack_archive(pred_path, pred_folder)

    if not os.path.exists(gold_folder):  # use cache if exists
        os.makedirs(gold_folder)
        shutil.unpack_archive(gold_path, gold_folder)

    pred_files = sorted(os.listdir(os.path.join(pred_folder, file_path)))
    gold_files = sorted(os.listdir(os.path.join(gold_folder, file_path)))

    if pred_files != gold_files:
        return 0., f"Archive file lists don't match - Expected: {gold_files}, Actual: {pred_files}"

    def get_compare_function():
        file_type = kwargs.pop('file_type', 'text')
        if file_type == 'text':
            from .vscode import compare_text_file
            return compare_text_file
        elif file_type == 'pdf':
            return compare_pdfs
        elif file_type == 'docx':
            from .docs import compare_docx_files
            return compare_docx_files
        elif file_type == 'ppt':
            from .slides import compare_pptx_files
            return compare_pptx_files
        elif file_type == 'image':
            from .vlc import compare_images
            return compare_images
        elif file_type == 'csv':
            from .table import compare_csv
            return compare_csv
        elif file_type == 'table':
            from .table import compare_table
            return compare_table
        elif file_type == 'audio':
            from .vlc import compare_audios
            return compare_audios
        elif file_type == 'video':
            from .vlc import compare_videos
            return compare_videos
        else:
            raise ValueError('[ERROR]: not support file type: %s' % file_type)

    score = 0
    compare_function = get_compare_function()
    file_results = []
    for f1, f2 in zip(pred_files, gold_files):
        fp1 = os.path.join(pred_folder, file_path, f1)
        fp2 = os.path.join(gold_folder, file_path, f2)
        file_score, file_reason = compare_function(fp1, fp2, **kwargs)
        score += file_score
        file_results.append(f"{f1}: {file_reason}")
    
    avg_score = score / len(pred_files)
    return avg_score, f"Archive comparison - Average score: {avg_score:.2%}, Files: {'; '.join(file_results)}"


def compare_htmls(html_path1: str, html_path2: str, **options) -> Tuple[float, str]:
    """
    Compare two HTML files.
    """
    try:
        with open(html_path1, 'r', encoding='utf-8') as inf:
            soup1 = BeautifulSoup(inf, 'lxml')
        with open(html_path2, 'r', encoding='utf-8') as inf:
            soup2 = BeautifulSoup(inf, 'lxml')
    except Exception as e:
        return 0., f"Error reading HTML files: {e}"
    
    ignore_sdnum = options.get("ignore_sdnum", None)

    def compare_elements(elem1, elem2):
        if not (isinstance(elem1, Tag) and isinstance(elem2, Tag)):
            if elem1 != elem2:
                logger.info("not the same")
            return elem1 == elem2, "Element types or values don't match"
        if elem1.name != elem2.name:
            logger.info("html name not match")
            return False, f"Tag names don't match: {elem1.name} vs {elem2.name}"
        if elem1.text.strip() != elem2.text.strip():
            logger.info("html text not match")
            return False, f"Text content doesn't match"
        if elem1.attrs != elem2.attrs:
            if ignore_sdnum:
                attrs1 = {k: v for k, v in elem1.attrs.items() if k != 'sdnum'}
                attrs2 = {k: v for k, v in elem2.attrs.items() if k != 'sdnum'}
                if attrs1 == attrs2:
                    return True, "Attributes match (ignoring sdnum)"
            logger.info("html attrs not match")
            logger.info(f"{elem1.attrs}")
            logger.info(f"{elem2.attrs}")
            return False, f"Attributes don't match"
        return True, "Elements match"

    for elem1, elem2 in zip(soup1.recursiveChildGenerator(), soup2.recursiveChildGenerator()):
        match, reason = compare_elements(elem1, elem2)
        if not match:
            logger.info("html not match")
            return 0., f"HTML mismatch: {reason}"
    return 1., "HTML files are identical"


def is_cookie_deleted(cookie_data, rule) -> Tuple[float, str]:
    """
    Check if the cookie is deleted.
    """

    if rule['type'] == 'domains':
        cookies_domains = [cookie[1] for cookie in cookie_data]
        for domain in rule['domains']:
            for cookies_domain in cookies_domains:
                if compare_urls(domain, cookies_domain):
                    return 0., f"Cookie still exists for domain: {domain}"
        return 1., f"All cookies deleted for domains: {rule['domains']}"
    else:
        raise TypeError(f"{rule['type']} not support yet!")


def is_shortcut_on_desktop(shortcuts: Dict[str, str], rule) -> Tuple[float, str]:
    """
    Check if the shortcut is on the desktop.
    """
    # fixme: if the name of the website changed in the future, this will not work; can be replaced with url
    if rule['type'] == 'name':
        for shortcut_path, shortcut_content in shortcuts.items():
            if "Name=" + rule['name'] + "\n" in shortcut_content:
                return 1., f"Shortcut with name '{rule['name']}' found on desktop"
        return 0.0, f"Shortcut with name '{rule['name']}' not found on desktop"
    elif rule['type'] == 'exec':
        for shortcut_path, shortcut_content in shortcuts.items():
            if "Exec=" + rule['exec'] + "\n" in shortcut_content:
                return 1., f"Shortcut with exec '{rule['exec']}' found on desktop"
        return 0.0, f"Shortcut with exec '{rule['exec']}' not found on desktop"
    elif rule['type'] == 'url':
        raise TypeError(f"{rule['type']} not support yet!")
    elif rule['type'] == 'id':
        raise TypeError(f"{rule['type']} not support yet!")
    else:
        raise TypeError(f"{rule['type']} not support yet!")


def check_history_deleted(history_data, rule) -> Tuple[float, str]:
    """
    Check if the history is deleted.
    """

    if rule['type'] == 'keywords':
        history_domains = [history[0] for history in history_data]
        for keyword in rule['keywords']:
            for history_domain in history_domains:
                if keyword in history_domain:
                    return 0., f"History still contains keyword: '{keyword}' in domain: {history_domain}"
        return 1., f"History successfully deleted for keywords: {rule['keywords']}"
    else:
        raise TypeError(f"{rule['type']} not support yet!")


def check_enabled_experiments(enabled_experiments, rule) -> Tuple[float, str]:
    """
    Check if the enabled experiments are as expected.
    """
    enabled_experiments_names = [experiment.split("@")[0] for experiment in enabled_experiments]

    if rule['type'] == 'names':
        if enabled_experiments_names == rule['names']:
            return 1., f"Enabled experiments match expected: {rule['names']}"
        else:
            return 0., f"Enabled experiments mismatch - Expected: {rule['names']}, Actual: {enabled_experiments_names}"
    else:
        raise TypeError(f"{rule['type']} not support yet!")


def check_font_size(font_size, rule) -> Tuple[float, str]:
    """
    Check if the font size is as expected.
    """

    default_font_size = font_size['default_font_size']
    if rule['type'] == 'value':
        if default_font_size == rule['value']:
            return 1., f"Font size matches expected value: {rule['value']}"
        else:
            return 0., f"Font size mismatch - Expected: {rule['value']}, Actual: {default_font_size}"
    elif rule['type'] == 'range':
        if rule['min'] < default_font_size < rule['max']:
            return 1., f"Font size {default_font_size} is within expected range: ({rule['min']}, {rule['max']})"
        else:
            return 0., f"Font size {default_font_size} is outside expected range: ({rule['min']}, {rule['max']})"
    else:
        raise TypeError(f"{rule['type']} not support yet!")


def is_added_to_steam_cart(active_tab_info, rule) -> Tuple[float, str]:
    """
    Check if the item is added to the Steam cart.
    """
    items = rule['items']

    content = active_tab_info['content']

    missing_items = []
    for item in items:
        if item not in content:
            missing_items.append(item)

    if missing_items:
        return 0., f"Items not found in Steam cart: {missing_items}"
    else:
        return 1., f"All items found in Steam cart: {items}"
