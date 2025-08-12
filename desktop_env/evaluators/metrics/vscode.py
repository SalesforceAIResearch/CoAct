import copy
import importlib.util
import json
import sys
import re
from typing import Dict, Tuple


def check_json_keybindings(actual: str, expected: str, **options) -> Tuple[float, str]:
    """
    Args:
        actual (str): path to result text file
        expected (str): expected dict{}

    Return:
        Tuple[float, str]: the score and reason
    """

    def direct_load_json(fp):
        try:
            with open(fp, 'r') as f:
                data = json.load(f)
            return data
        except:
            return None

    def skip_first_line_load_json(fp):
        try:
            with open(fp, 'r') as f:
                f.readline()
                data = json.load(f)
            return data
        except:
            return None

    for func in [direct_load_json, skip_first_line_load_json]:
        data = func(actual)
        if data is not None and type(data) == list:
            break
    else:
        return 0.0, "Failed to load JSON data or data is not a list"
    expected = expected['expected']
    if expected in data:
        return 1.0, "Expected keybinding found in the list"
    else:
        return 0.0, f"Expected keybinding not found in the list"


def check_json_settings(actual: str, expected: str, **options) -> Tuple[float, str]:
    """
    Args:
        actual (str): path to result text file
        expected (dict): expected dict{}, containing key "expect"

    Return:
        Tuple[float, str]: the score and reason
    """
    if not actual:
        return 0., "No actual file path provided"

    try:
        with open(actual, 'r') as f:
            data = json.load(f)
    except Exception as e:
        return 0.0, f"Failed to load JSON file: {str(e)}"

    expect = expected['expected']
    
    # Check if all expected key-value pairs are in the actual data
    for key, value in expect.items():
        if key not in data:
            return 0.0, f"Expected key '{key}' not found in settings"
        if data[key] != value:
            return 0.0, f"Key '{key}' has value '{data[key]}' but expected '{value}'"
    
    return 1.0, "All expected settings match"


def compare_text_file(actual: str, expected: str, **options) -> Tuple[float, str]:
    """
    Args:
        actual (str): path to result text file
        expected (str): path to gold text file

    Return:
        Tuple[float, str]: the score and reason
    """
    if not actual:
        return 0., "No actual file path provided"

    try:
        with open(actual) as f1:
            actual_text = f1.read()
    except Exception as e:
        return 0.0, f"Failed to read actual file: {str(e)}"
    
    try:
        with open(expected) as f2:
            expected_text = f2.read()
    except Exception as e:
        return 0.0, f"Failed to read expected file: {str(e)}"

    ignore_blanks = options.get('ignore_blanks', False)
    if ignore_blanks:
        actual_text = re.sub(r'[\t\n]', ' ', actual_text).strip()
        actual_text = re.sub(r'\s+', ' ', actual_text)
        expected_text = re.sub(r'[\t\n]', ' ', expected_text).strip()
        expected_text = re.sub(r'\s+', ' ', expected_text)

    ignore_case = options.get('ignore_case', False)
    if ignore_case:
        actual_text = actual_text.lower()
        expected_text = expected_text.lower()

    if actual_text == expected_text:
        return 1.0, "Text files match exactly"
    return 0.0, "Text files do not match"

import zipfile
from difflib import SequenceMatcher
import PyPDF2

def compare_pdf_content(content1, content2, text_similarity_threshold):
    def extract_text_from_pdf(content):
        with open("temp.pdf", "wb") as temp_pdf:
            temp_pdf.write(content)
        with open("temp.pdf", "rb") as temp_pdf:
            pdf_reader = PyPDF2.PdfReader(temp_pdf)
            text = ''
            for page_num in range(len(pdf_reader.pages)):
                page = pdf_reader.pages[page_num]
                text += page.extract_text()
        return text

    text1 = extract_text_from_pdf(content1)
    text2 = extract_text_from_pdf(content2)

    similarity_ratio = SequenceMatcher(None, text1, text2).ratio()

    return similarity_ratio >= text_similarity_threshold

def compare_zip_files(actual: str, expected: str, **options) -> Tuple[float, str]:
    """
    Args:
        actual (str): path to result zip file
        expected (str): path to gold zip file

    Return:
        Tuple[float, str]: the score and reason
    """
    if not actual:
        return 0., "No actual file path provided"

    try:
        with zipfile.ZipFile(actual, 'r') as zip_file1, zipfile.ZipFile(expected, 'r') as zip_file2:
            file_list1 = set(zip_file1.namelist())
            file_list2 = set(zip_file2.namelist())

            if file_list1 != file_list2:
                missing_in_actual = file_list2 - file_list1
                extra_in_actual = file_list1 - file_list2
                reason = "File lists don't match. "
                if missing_in_actual:
                    reason += f"Missing: {missing_in_actual}. "
                if extra_in_actual:
                    reason += f"Extra: {extra_in_actual}"
                return 0.0, reason
            
            for file_name in file_list1:
                content1 = zip_file1.read(file_name)
                content2 = zip_file2.read(file_name)

                if file_name.lower().endswith('.pdf'):
                    if not compare_pdf_content(content1, content2, 0.95):
                        return 0.0, f"PDF content mismatch in file: {file_name}"
                elif content1 != content2:
                    return 0.0, f"Content mismatch in file: {file_name}"
    except Exception as e:
        return 0.0, f"Error comparing zip files: {str(e)}"
    
    return 1.0, "All files in zip archives match"


def compare_config(actual: str, rules: Dict, **options) -> Tuple[float, str]:
    if not actual:
        return 0., "No actual file path provided"

    try:
        with open(actual) as f1:
            actual_text = f1.read()
    except Exception as e:
        return 0.0, f"Failed to read config file: {str(e)}"

    if actual_text == rules['expected']:
        return 1.0, "Config matches expected value"
    return 0.0, "Config does not match expected value"


def compare_answer(actual: str, rules: Dict, **options) -> Tuple[float, str]:
    """
    Args:
        actual (str): result string
        expected (str): gold string

    Return:
        Tuple[float, str]: the score and reason
    """
    if not actual:
        return 0., "No actual answer provided"

    if actual == rules['expected']:
        return 1.0, "Answer matches expected value"

    # TODO: can use text embedding to get non-zero return
    return 0.0, f"Answer '{actual}' does not match expected '{rules['expected']}'"


def is_extension_installed(actual: str, rules: Dict, **options) -> Tuple[float, str]:
    if rules['type'] == 'contain':
        if rules['expected'] in actual:
            return 1.0, f"Extension '{rules['expected']}' found in the list"
        return 0.0, f"Extension '{rules['expected']}' not found in the list"
    elif rules['type'] == 'not_contain':
        if rules['expected'] not in actual:
            return 1.0, f"Extension '{rules['expected']}' correctly not in the list"
        return 0.0, f"Extension '{rules['expected']}' found but should not be present"
    else:
        raise NotImplementedError(f"Unknown rule type: {rules['type']}")


def check_python_file_by_test_suite(actual_files, test_file, **options) -> Tuple[float, str]:
    """Check the python file by running the test suite in the given test file."""

    test_function_name = options.get('test_function_name', 'test')
    # Create a unique module name, it can be arbitrary but must be unique in the current runtime environment
    module_name = 'dynamic_module'

    try:
        # Load the module from the given file path
        spec = importlib.util.spec_from_file_location(module_name, test_file)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module  # Add the loaded module to sys.modules
        spec.loader.exec_module(module)  # Execute the module to make its content available

        # Retrieve the function by name from the loaded module and execute it
        test_function = getattr(module, test_function_name)
        if test_function():
            return 1.0, "Test suite passed successfully"
        else:
            return 0.0, "Test suite failed"
    except AttributeError:
        return 0.0, f"Test function '{test_function_name}' not found in test file"
    except Exception as e:
        return 0.0, f"Error running test suite: {str(e)}"


def check_python_file_by_gold_file(actual_files, gold_file: str, **options) -> Tuple[float, str]:
    pass


def check_html_background_image(src_path: str, rule: Dict = None) -> Tuple[float, str]:
    """
    Check if the background image is correctly set.
    multi-app:bb7db4c2-30b5-4be7-8dd7-b8c4ec7d3108
    """
    if not src_path:
        return 0.0, "No source file path provided"

    try:
        from bs4 import BeautifulSoup
        with open(src_path, 'r') as f:
            html_content = f.read()
        soup = BeautifulSoup(html_content, 'html.parser')
        styles = soup.find_all('style')
        expected_style = f'background-image: url(\'{rule["value"]}\')'
        for style in styles:
            if expected_style in style.text:
                return 1.0, f"Background image correctly set to '{rule['value']}'"
        return 0.0, f"Background image '{rule['value']}' not found in styles"
    except Exception as e:
        return 0.0, f"Error checking HTML file: {str(e)}"


def compare_result_files(src_path, tgt_path) -> Tuple[float, str]:
    """
    Compare whether the content of two files are the same.
    multi-app:7f35355e-02a6-45b5-b140-f0be698bcf85
    """
    if not src_path or not tgt_path:
        return 0.0, "Missing source or target file path"

    try:
        with open(src_path, 'r') as f:
            src_content = f.read().strip()
        with open(tgt_path, 'r') as f:
            tgt_content = f.read().strip()
    except Exception as e:
        return 0.0, f"Error reading files: {str(e)}"
    
    try:
        # Compare the content as numbers
        tgt_content_num = float(tgt_content)
        if tgt_content in src_content:
            # If the content of tgt is in src, return 1.0 since output src might be
            # a superset(language description+number) of tgt
            return 1.0, "Target content found in source (numeric match)"
        src_content_num = float(src_content)
        if abs(src_content_num - tgt_content_num) < 1e-4:
            return 1.0, f"Numeric values match within tolerance ({src_content_num} ≈ {tgt_content_num})"
        return 0.0, f"Numeric values differ: {src_content_num} vs {tgt_content_num}"
    except:
        if src_content == tgt_content:
            return 1.0, "File contents match exactly"
    return 0.0, "File contents do not match"
