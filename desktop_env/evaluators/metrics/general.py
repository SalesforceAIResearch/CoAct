import csv
import datetime
import difflib
import functools
import json
import logging
import operator
import os
import re
import sqlite3
from numbers import Number
from typing import Callable, Any, Union, Tuple
from typing import Dict, List, Pattern

import lxml.etree
import pdfplumber
import yaml
from docx import Document
from lxml.cssselect import CSSSelector
from lxml.etree import _Element
from rapidfuzz import fuzz

from desktop_env.evaluators.metrics.utils import _match_record, _match_value_to_rule

logger = logging.getLogger("desktopenv.metric.general")


def check_include_exclude(result: str, rules: Dict[str, List[str]]) -> Tuple[float, str]:
    if result is None:
        return 0., "Result is None"

    print(result, rules)
    include = rules.get("include", [])
    exclude = rules.get("exclude", [])
    if all(r in result for r in include) and all(r not in result for r in exclude):
        return 1., f"All include rules {include} found and no exclude rules {exclude} found"
    else:
        missing_include = [r for r in include if r not in result]
        found_exclude = [r for r in exclude if r in result]
        reason = []
        if missing_include:
            reason.append(f"Missing include rules: {missing_include}")
        if found_exclude:
            reason.append(f"Found exclude rules: {found_exclude}")
        return 0., "; ".join(reason)


def exact_match(result, rules) -> Tuple[float, str]:
    expect = rules["expected"]
    print(result, expect)

    if result == expect:
        return 1., f"Exact match: result '{result}' equals expected '{expect}'"
    else:
        return 0., f"No match: result '{result}' does not equal expected '{expect}'"

def match_in_list(result, rules) -> Tuple[float, str]:
    expect = rules["expected"]
    print(result, expect)

    if result in expect:
        return 1., f"Result '{result}' found in expected list {expect}"
    else:
        return 0., f"Result '{result}' not found in expected list {expect}"

def literal_match(result: Any, expected: Any, **options) -> Tuple[float, str]:
    literal_type = options.get('type', 'str')
    if literal_type == 'str':
        ignore_case = options.get('ignore_case', False)
        if ignore_case:
            match = str(result).lower() == str(expected).lower()
            if match:
                return 1., f"Case-insensitive match: '{result}' equals '{expected}'"
            else:
                return 0., f"Case-insensitive mismatch: '{result}' does not equal '{expected}'"
        else:
            match = str(result) == str(expected)
            if match:
                return 1., f"Exact string match: '{result}' equals '{expected}'"
            else:
                return 0., f"String mismatch: '{result}' does not equal '{expected}'"
    elif literal_type == 'list':
        if type(result) not in [list, tuple] or type(expected) not in [list, tuple]:
            return 0., f"Type mismatch: result type {type(result)} or expected type {type(expected)} is not list/tuple"
        if len(result) != len(expected):
            return 0., f"Length mismatch: result length {len(result)} != expected length {len(expected)}"
        ignore_case = options.get('ignore_case', False)
        result_processed = [str(s) for s in result] if not ignore_case else [str(s).lower() for s in result]
        expected_processed = [str(s) for s in expected] if not ignore_case else [str(s).lower() for s in expected]
        if result_processed == expected_processed:
            return 1., f"List match: {result} equals {expected}" + (" (case-insensitive)" if ignore_case else "")
        else:
            return 0., f"List mismatch: {result} does not equal {expected}" + (" (case-insensitive)" if ignore_case else "")
    else:
        raise NotImplementedError(f"Type {literal_type} not supported")


def is_in_list(result, rules) -> Tuple[float, str]:
    expect = rules["expected"]
    if expect in result:
        return 1., f"Expected value '{expect}' found in result list"
    else:
        return 0., f"Expected value '{expect}' not found in result list"


def diff_text_file(result: str, expect: str) -> Tuple[float, str]:
    if result is None:
        return 0., "Result file path is None"

    try:
        with open(result) as f:
            result_lines: List[str] = f.read().splitlines()
        with open(expect) as f:
            expected_lines: List[str] = f.read().splitlines()
        ratio = difflib.SequenceMatcher(a=result_lines, b=expected_lines).ratio()
        return ratio, f"Text file similarity ratio: {ratio:.2%}"
    except Exception as e:
        return 0., f"Error reading files: {str(e)}"


def fuzzy_match(result, rules) -> Tuple[float, str]:
    expect = rules["expected"]
    ratio = fuzz.ratio(result, expect) / 100.
    return ratio, f"Fuzzy match ratio: {ratio:.2%} between '{result}' and '{expect}'"


def fuzzy_place_math(result_file_path, rules) -> Tuple[float, str]:
    if result_file_path is None:
        return 0., "Result file path is None"
    expect = rules["expected"]  # a list of possible answers
    try:
        # read list.docx, and get all texts out, overlook blank lines, remove blanks before and after each line
        doc = Document(result_file_path)
        words_list = []
        for para in doc.paragraphs:
            words_list.extend(para.text.split())
        fuzzy_score_list = []
        for word in words_list:
            max_score = 0
            best_match = None
            for ans in expect:
                score = fuzz.ratio(word, ans) / 100
                if score > max_score:
                    max_score = score
                    best_match = ans
            fuzzy_score_list.append(max_score)
        if len(fuzzy_score_list) != 3:
            return 0., f"Expected 3 words but found {len(fuzzy_score_list)} words"
        avg_score = sum(fuzzy_score_list) / 3
        return avg_score, f"Average fuzzy match score: {avg_score:.2%} for 3 words"
    except Exception as e:
        return 0., f"Error processing document: {str(e)}"


def check_csv(result: str, rules: Dict[str, List[Dict[str, str]]]) -> Tuple[float, str]:
    """
    Args:
        result (str): path to csv file
        rules (Dict[str, List[Dict[str, str]]]): dict like
          {
            "expect": [{key: value}]
            "unexpect": [{key: value}]
          }

    Returns:
        Tuple[float, str]
    """

    if result is None:
        return 0., "Result file path is None"

    try:
        expect_metrics = [False] * len(rules.get("expect", []))
        unexpect_metric = True
        with open(result) as f:
            reader = csv.DictReader(f)

            for rcd in reader:
                for i, r in enumerate(rules.get("expect", [])):
                    expect_metrics[i] = expect_metrics[i] or _match_record(r, rcd)
                unexpect_metric = unexpect_metric and not any(_match_record(r, rcd) for r in rules.get("unexpect", []))
        
        if all(expect_metrics) and unexpect_metric:
            return 1., "CSV check: All expect metrics matched and no unexpect metrics found"
        else:
            reasons = []
            if not all(expect_metrics):
                unmatched = [i for i, matched in enumerate(expect_metrics) if not matched]
                reasons.append(f"Unmatched expect rules at indices: {unmatched}")
            if not unexpect_metric:
                reasons.append("Found unexpected records")
            return 0., "; ".join(reasons)
    except Exception as e:
        return 0., f"Error reading CSV file: {str(e)}"


def check_list(result: str, rules: Dict[str, List[str]]) -> Tuple[float, str]:
    """
    Args:
        result (str): path to list file
        rules (Dict[str, List[str]]): dict like
          {
            "expect": list of str as regexes
            "unexpect": list of str as regexes
          }

    Returns:
        Tuple[float, str]
    """

    if result is None:
        return 0., "Result file path is None"

    try:
        expect_patterns: List[Pattern[str]] = [re.compile(ptt) for ptt in rules.get("expect", [])]
        unexpect_patterns: List[Pattern[str]] = [re.compile(ptt) for ptt in rules.get("unexpect", [])]

        expect_metrics = [False] * len(expect_patterns)
        unexpect_metric = True
        with open(result) as f:
            for l in f:
                for i, r in enumerate(expect_patterns):
                    expect_metrics[i] = expect_metrics[i] or (r.search(l) is not None)
                unexpect_metric = unexpect_metric and all(r.search(l) is None for r in unexpect_patterns)
        
        if all(expect_metrics) and unexpect_metric:
            return 1., "List check: All expect patterns matched and no unexpect patterns found"
        else:
            reasons = []
            if not all(expect_metrics):
                unmatched = [rules.get("expect", [])[i] for i, matched in enumerate(expect_metrics) if not matched]
                reasons.append(f"Unmatched expect patterns: {unmatched}")
            if not unexpect_metric:
                reasons.append("Found unexpected patterns")
            return 0., "; ".join(reasons)
    except Exception as e:
        return 0., f"Error reading list file: {str(e)}"


_accessibility_ns_map = {
    "ubuntu": {
        "st": "https://accessibility.ubuntu.example.org/ns/state",
        "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
        "cp": "https://accessibility.ubuntu.example.org/ns/component",
        "doc": "https://accessibility.ubuntu.example.org/ns/document",
        "docattr": "https://accessibility.ubuntu.example.org/ns/document/attributes",
        "txt": "https://accessibility.ubuntu.example.org/ns/text",
        "val": "https://accessibility.ubuntu.example.org/ns/value",
        "act": "https://accessibility.ubuntu.example.org/ns/action",
    },
    "windows": {
        "st": "https://accessibility.windows.example.org/ns/state",
        "attr": "https://accessibility.windows.example.org/ns/attributes",
        "cp": "https://accessibility.windows.example.org/ns/component",
        "doc": "https://accessibility.windows.example.org/ns/document",
        "docattr": "https://accessibility.windows.example.org/ns/document/attributes",
        "txt": "https://accessibility.windows.example.org/ns/text",
        "val": "https://accessibility.windows.example.org/ns/value",
        "act": "https://accessibility.windows.example.org/ns/action",
        "class": "https://accessibility.windows.example.org/ns/class"
    },
    "macos": {
        "st": "https://accessibility.macos.example.org/ns/state",
        "attr": "https://accessibility.macos.example.org/ns/attributes",
        "cp": "https://accessibility.macos.example.org/ns/component",
        "doc": "https://accessibility.macos.example.org/ns/document",
        "txt": "https://accessibility.macos.example.org/ns/text",
        "val": "https://accessibility.macos.example.org/ns/value",
        "act": "https://accessibility.macos.example.org/ns/action",
        "role": "https://accessibility.macos.example.org/ns/role",
    }

}

def check_accessibility_tree(result: str, rules: List[Dict[str, Any]], osname: str = "ubuntu") -> Tuple[float, str]:
    """
    Args:
        result (str): XML of GNOME Accessibility Tree
        rules (List[Dict[str, Any]]): list of dict like
          {
            "selectors": list of str as CSS selectors, will be connected by ", "
              to form a composite selector. Only one from `selectors` and
              `xpath` is needed. If both are present, `xpath` takes the
              priority.
            "xpath": str as xpath. Only one from `selectors` and `xpath` is
              needed. If both are present, `xpath` takes the priority.
            "text": str as the expected text content of the selected element.
            "exact": bool specifying whether exact match or fuzzy match should
              be performed. defaults to True.
          }
        osname (str): "ubuntu" | "windows" | "macos". "ubuntu" by default.

    Returns:
        Tuple[float, str]
    """

    a11y_ns_map = _accessibility_ns_map[osname]

    try:
        at: _Element = lxml.etree.fromstring(result)
        total_match_score = 1.
        reasons = []
        
        for r in rules:
            if "xpath" in r:
                elements: List[_Element] = at.xpath(r["xpath"], namespaces=a11y_ns_map)
            elif "selectors" in r:
                selector = CSSSelector(", ".join(r["selectors"]), namespaces=a11y_ns_map)
                elements: List[_Element] = selector(at)
            else:
                raise ValueError("At least one of xpath and selectors is required")

            if len(elements) == 0:
                logger.info("No elements: %s", r["xpath"] if "xpath" in r else r["selectors"])
                return 0., f"No elements found for rule: {r['xpath'] if 'xpath' in r else r['selectors']}"

            if "text" in r:
                match_func: Callable[[str], Number] = functools.partial(operator.eq if r.get("exact", True) \
                                                                            else (lambda a, b: fuzz.ratio(a, b) / 100.)
                                                                        , r["text"]
                                                                        )
                match_score: Number = 0
                for elm in elements:
                    match_score = max(match_score, match_func(elm.text or ""))
            else:
                match_score = 1.
            total_match_score *= match_score
            
            if match_score < 1:
                reasons.append(f"Partial match ({match_score:.2%}) for rule: {r}")

        if total_match_score == 1:
            return 1., "Accessibility tree check: All rules fully matched"
        else:
            return total_match_score, f"Accessibility tree check: Total match score: {total_match_score:.2%}. " + "; ".join(reasons)
    except Exception as e:
        return 0., f"Error parsing accessibility tree: {str(e)}"


# def check_existence(result: str, *args) -> float:
# return 1. - (result is None)

def run_sqlite3(result: str, rules: Dict[str, Any]) -> Tuple[float, str]:
    try:
        connection: sqlite3.Connection = sqlite3.connect(result)
        cursor: sqlite3.Cursor = connection.execute(rules["sql"])
        result_value = cursor.fetchone()[0] or 0
        connection.close()
        return float(result_value), f"SQLite3 query executed successfully, result: {result_value}"
    except Exception as e:
        return 0., f"Error executing SQLite3 query: {str(e)}"


def check_json(result: str, rules: Dict[str, List[Dict[str, Union[List[str], str]]]], is_yaml: bool = False) -> Tuple[float, str]:
    """
    Args:
        result (str): path to json file
        rules (Dict[str, List[Dict[str, Union[List[str], str]]]]): dict like
          {
            "expect": [
                {
                    "key": list of str
                    "method": str
                    "ref": something
                }
            ],
            "unexpect": <the same as `expect`
          }
        is_yaml (bool): yaml rather than json

    Returns:
        Tuple[float, str]
    """

    if result is None:
        return 0., "Result file path is None"
    
    try:
        with open(result) as f:
            if is_yaml:
                result: Dict[str, Any] = yaml.load(f, Loader=yaml.Loader)
            else:
                result: Dict[str, Any] = json.load(f)

        expect_rules = rules.get("expect", {})
        unexpect_rules = rules.get("unexpect", {})

        metric = True
        reasons = []
        
        for r in expect_rules:
            value = result
            for k in r["key"]:
                try:
                    value = value[k]
                except KeyError:
                    return 0., f"Expected key path {r['key']} not found in result JSON"
            if not _match_value_to_rule(value, r):
                metric = False
                reasons.append(f"Expect rule failed for key path {r['key']}")
                
        for r in unexpect_rules:
            value = result
            for k in r["key"]:
                try:
                    value = value[k]
                except KeyError:
                    value = None
                    break
            if value is not None and _match_value_to_rule(value, r):
                metric = False
                reasons.append(f"Unexpect rule matched for key path {r['key']}")
                
        if metric:
            return 1., "JSON check: All expect rules matched and no unexpect rules found"
        else:
            return 0., "JSON check failed: " + "; ".join(reasons)
    except Exception as e:
        return 0., f"Error reading/parsing {'YAML' if is_yaml else 'JSON'} file: {str(e)}"


def check_direct_json_object(result, rules) -> Tuple[float, str]:
    """
    One of the most commonly used function to evalute.
    Compare two json objects directly.
    """
    logger.info(f"[DEBUG] check_direct_json_object called with result: {result}")
    logger.info(f"[DEBUG] check_direct_json_object called with rules: {rules}")
    
    if isinstance(result, str):
        # remove blanks before and after result
        result = result.strip()
        # replace all ' with "
        result = result.replace("'", '"')
        # load json object
        result = json.loads(result)
        
    logger.info(f"[DEBUG] Processed result: {result}")
    
    if result is None:
        logger.info("[DEBUG] Result is None, returning 0.0")
        return 0., "Result is None"
    
    # Check if expected value contains evaluation failure indicator
    try:
        expected_json = rules.get("expected", {})
        if expected_json:
            for key, value in expected_json.items():
                if value == "__EVALUATION_FAILED__":
                    logger.error(f"[DEBUG] Expected value for key '{key}' indicates evaluation failure, returning 0.0")
                    return 0., f"Expected value for key '{key}' indicates evaluation failure"
    except Exception as e:
        logger.error(f"[DEBUG] Error checking for evaluation failure indicator: {e}")
        return 0., f"Error checking for evaluation failure indicator: {e}"
    try:
        expect_in_result = rules.get("expect_in_result", False)
        logger.info(f"[DEBUG] expect_in_result: {expect_in_result}")
        
        if not expect_in_result:
            expected_json = rules["expected"]
            logger.info(f"[DEBUG] Expected JSON: {expected_json}")
            
            for key in expected_json.keys():
                expected_value = expected_json.get(key)
                actual_value = result.get(key)
                logger.info(f"[DEBUG] Checking key '{key}': expected='{expected_value}', actual='{actual_value}'")
                
                if expected_json.get("ignore_list_order", False):
                    expected_value = sorted(expected_value)
                    result_value = sorted(result.get(key))
                    logger.info(f"[DEBUG] Comparing lists (sorted): expected={expected_value}, actual={result_value}")
                    if expected_value != result_value:
                        logger.info(f"[DEBUG] List comparison failed for key '{key}', returning 0.0")
                        return 0., f"List comparison failed for key '{key}'"
                else:
                    if expected_value != actual_value:
                        logger.info(f"[DEBUG] Value comparison failed for key '{key}': expected='{expected_value}', actual='{actual_value}', returning 0.0")
                        return 0., f"Value comparison failed for key '{key}'"
                    else:
                        logger.info(f"[DEBUG] Value comparison passed for key '{key}'")
                        
            logger.info("[DEBUG] All comparisons passed, returning 1.0")
            return 1., "All direct JSON object comparisons passed"
        else:
            expected_json = rules["expected"]
            logger.info(f"[DEBUG] Expected JSON (expect_in_result mode): {expected_json}")

            for key in expected_json.keys():
                if isinstance(expected_json.get(key), list):
                    flag = 0
                    expected_value_list = expected_json.get(key)
                    logger.info(f"[DEBUG] Checking list key '{key}': expected_list={expected_value_list}, actual='{result.get(key)}'")
                    for each_expected_value in expected_value_list:
                        # Handle both list and string cases
                        if isinstance(result.get(key), list) and each_expected_value in result.get(key):
                            flag = 1
                            logger.info(f"[DEBUG] Found expected value '{each_expected_value}' in result list for key '{key}'")
                            break
                        elif isinstance(result.get(key), str) and each_expected_value == result.get(key):
                            flag = 1
                            logger.info(f"[DEBUG] Found expected value '{each_expected_value}' matches result string for key '{key}'")
                            break
                    if flag == 0:
                        logger.info(f"[DEBUG] No expected values found in result for key '{key}', returning 0.0")
                        return 0., f"No expected values found in result for key '{key}'"
                elif isinstance(expected_json.get(key), str):
                    expected_str = expected_json.get(key)
                    actual_str = result.get(key)
                    logger.info(f"[DEBUG] Checking string key '{key}': expected='{expected_str}', actual='{actual_str}'")
                    if expected_str not in actual_str:
                        logger.info(f"[DEBUG] Expected string '{expected_str}' not found in actual string '{actual_str}' for key '{key}', returning 0.0")
                        return 0., f"Expected string '{expected_str}' not found in actual string '{actual_str}' for key '{key}'"
                else:
                    logger.debug("check_direct_json_object: expected value type not supported")
                    return 0., "Expected value type not supported"
            logger.info("[DEBUG] All expect_in_result comparisons passed, returning 1.0")
            return 1., "All expect_in_result direct JSON object comparisons passed"
    except Exception as e:
        logger.debug(f"check_direct_json_object: result is not a valid json object, error: {e}")
        return 0., f"Result is not a valid json object: {e}"


def compare_time_in_speedtest_results(speedtest_result_path, time_diff) -> Tuple[float, str]:
    if not speedtest_result_path:
        return 0., "Speedtest result path is None"

    # open the speedtest results file(csv)
    #date_col = None
    try:
        with open(speedtest_result_path, 'r') as f:
            for i, line in enumerate(f):
                if i == 1:
                    date = line.split(',')[1]
                    break
            now_date_time = datetime.datetime.now().strftime('%H:%M')
            date_time = date[-5:]
            # compare the date time with the current date time, if time diff less than time_diff para, then return true
            time_diff_minutes = abs((datetime.datetime.strptime(date_time, '%H:%M') - datetime.datetime.strptime(now_date_time, '%H:%M')).total_seconds()) / 60
            if time_diff_minutes < int(time_diff):
                return 1., f"Time difference {time_diff_minutes:.1f} minutes is within tolerance of {time_diff} minutes"
            else:
                return 0., f"Time difference {time_diff_minutes:.1f} minutes exceeds tolerance of {time_diff} minutes"
    except Exception as e:
        logger.debug(f"compare_time_in_speedtest_results: file not found or not readable: {e}")
        return 0., f"Error reading speedtest results file: {str(e)}"


def is_included_all_json_objects(gold_file_path, result_file_path) -> Tuple[float, str]:
    if not gold_file_path or not result_file_path:
        return 0., "Gold file path or result file path is None"

    print("gold_file_path: ")
    print(gold_file_path)
    print("result_file_path: ")
    print(result_file_path)
    
    try:
        # two json file, check if all the key-value pair in gold_file_path is included in result_file_path
        with open(gold_file_path, 'r') as f:
            gold_json = json.load(f)
        with open(result_file_path, 'r') as fr:
            result_json = json.load(fr)
        
        missing_or_mismatched = []
        for key in gold_json.keys():
            if key not in result_json.keys():
                missing_or_mismatched.append(f"Key '{key}' missing")
            elif gold_json[key] != result_json[key]:
                missing_or_mismatched.append(f"Key '{key}' value mismatch: expected '{gold_json[key]}', got '{result_json[key]}'")
        
        if missing_or_mismatched:
            return 0., "JSON object inclusion check failed: " + "; ".join(missing_or_mismatched)
        else:
            return 1., "All key-value pairs from gold JSON are included in result JSON"
    except Exception as e:
        return 0., f"Error reading/parsing JSON files: {str(e)}"


def is_gold_text_included_in_pdf(pdf_file_path, gold_text_path) -> Tuple[float, str]:
    if not gold_text_path or not pdf_file_path:
        return 0., "Gold text path or PDF file path is None"

    print("gold_text_path: ")
    print(gold_text_path)
    print("pdf_file_path: ")
    print(pdf_file_path)
    
    try:
        # gold file is a json file, we need to check all the value in json are included in pdf file.
        with open(gold_text_path, 'r') as f:
            gold_json = json.load(f)
        with pdfplumber.open(pdf_file_path) as pdf:
            text = ''
            for page in pdf.pages:
                text += page.extract_text()
        false_list = []
        for key in gold_json.keys():
            if gold_json[key] not in text:
                false_list.append(key)
        if len(false_list) > 0:
            print("false_list: ")
            print(false_list)
            missing_texts = [f"'{gold_json[key]}' (key: {key})" for key in false_list[:3]]  # Show first 3
            if len(false_list) > 3:
                missing_texts.append(f"and {len(false_list) - 3} more...")
            return 0., f"Expected text not found in PDF: {', '.join(missing_texts)}"
        else:
            return 1., "All expected text from gold JSON found in PDF"
    except Exception as e:
        return 0., f"Error processing PDF or gold text file: {str(e)}"


def file_contains(file_path, config) -> Tuple[float, str]:
    # file_path ends with .txt
    if not file_path:
        return 0., "File path is None"
    try:
        with open(file_path, 'r') as f:
            file_text = f.read()
        missing_texts = []
        for text in config["expected"]:
            if text not in file_text:
                logger.debug(f"file_contains: {text} not found in {file_path}")
                missing_texts.append(f"'{text}'")
        if missing_texts:
            return 0., f"Expected text not found in file: {', '.join(missing_texts[:3])}" + (" and more..." if len(missing_texts) > 3 else "")
        else:
            return 1., "All expected text found in file"
    except Exception as e:
        logger.debug(f"file_contains: file not found or not readable: {e}")
        return 0., f"Error reading file: {str(e)}"


def check_line_number(file_path, line_number) -> Tuple[float, str]:
    # check if file_path exists
    if file_path is None or not os.path.isfile(file_path):
        return 0., "File path is None or file does not exist"
    timeRegex = "([01]\\d|2[0-3]):[0-5]\\d:([0-5]\\d|60)"
    # check if the string that matches the timeRegex in this txt file equals to line_number["expected"]
    try:
        with open(file_path, 'r') as f:
            line_count = 0
            for line in f:
                if re.search(timeRegex, line):
                    line_count += 1
        # if line_count equals to line_number["expected"], return 1, else return 0
        expected_count = int(line_number["expected"])
        if line_count == expected_count:
            return 1., f"Line count matches: found {line_count} lines with time pattern"
        else:
            return 0., f"Line count mismatch: expected {expected_count}, found {line_count} lines with time pattern"
    except Exception as e:
        logger.debug(f"check_line_number: file not found or not readable: {e}")
        return 0., f"Error reading file: {str(e)}"


def compare_terminal_and_txt(txt_file_path, terminal_output) -> Tuple[float, str]:
    if not txt_file_path or not terminal_output:
        return 0., "Text file path or terminal output is None"

    try:
        # read txt file content
        with open(txt_file_path, 'r') as f:
            txt_file_content = f.read()
        # compare terminal output with txt file content
        if terminal_output == txt_file_content:
            return 1., "Terminal output matches text file content exactly"
        else:
            # Provide some context about the mismatch
            len_diff = len(terminal_output) - len(txt_file_content)
            return 0., f"Terminal output does not match text file content (length difference: {len_diff:+d} characters)"
    except Exception as e:
        return 0., f"Error reading text file: {str(e)}"


def compare_python_pure_text(py_file_path, gold_file_path) -> Tuple[float, str]:
    if not py_file_path or not gold_file_path:
        return 0., "Python file path or gold file path is None"

    def _normalize(text):
        """
        Minimal normalization - only handle basic formatting:
        - Skip obvious file metadata (encoding, shebang) at the beginning
        - Normalize whitespace and indentation
        - Remove empty lines
        
        This preserves any content that shouldn't be there (like markdown)
        so it can be detected as an error.
        """
        lines = text.splitlines()
        result_lines = []
        i = 0
        
        # Only skip obvious metadata at the very beginning
        while i < len(lines) and i < 3:  # Check only first 3 lines
            stripped = lines[i].strip()
            
            if (stripped.startswith('#!') or
                stripped.startswith('# -*- coding:') or
                stripped.startswith('# coding:') or
                stripped.startswith('# coding=')):
                i += 1
                continue
            
            break
        
        # Process all remaining lines with minimal filtering
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            
            if stripped:  # Keep all non-empty lines
                normalized = line.expandtabs(4).rstrip()
                result_lines.append(normalized)
            
            i += 1
        
        return '\n'.join(result_lines)

    try:
        with open(py_file_path, 'r', encoding='utf-8') as file1:
            user_content = file1.read()
        with open(gold_file_path, 'r', encoding='utf-8') as file2:
            gold_content = file2.read()
        
        # Apply different normalization strategies
        user_normalized = _normalize(user_content)
        gold_normalized = _normalize(gold_content)
        
        if user_normalized == gold_normalized:
            return 1., "Python file content matches gold file content"
        else:
            return 0., "Python file content does not match gold file content"
            
    except (FileNotFoundError, IOError, UnicodeDecodeError) as e:
        logger.debug(f"compare_python_pure_text: Error reading files - {e}")
        return 0., f"Error reading files - {e}"
    except Exception as e:
        logger.debug(f"compare_python_pure_text: Unexpected error - {e}")
        return 0., f"Unexpected error - {e}"
