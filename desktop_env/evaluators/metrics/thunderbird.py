import json
import logging
import re
from typing import List, Pattern, Dict, Match, Tuple
from typing import Union, Any, TypeVar, Callable

from .utils import _match_record
from .utils import _match_value_to_rule as _match_pref

logger = logging.getLogger("desktopenv.metric.thunderbird")

V = TypeVar("Value")

_pref_pattern: Pattern[str] = re.compile(r'^user_pref\("(?P<key>(?:[^"]|\\")+)\", (?P<val>.+)\);$');


def check_thunderbird_prefs(result: str, rule: Dict[str, Dict[str, Dict[str, Any]]]) -> Tuple[float, str]:
    """
    Args:
        result (str): path to result file
        rule (Dict[str, Dict[str, Dict[str, Any]]]): dict like
          {
            "expect": {
                str: {
                    "method": str
                    "ref": something
                }
            }
            "unexpect": {
                str: {
                    "method": str
                    "ref": something
                }
            }
          }

    Returns:
        Tuple[float, str]: (score, reason)
    """

    if result is None:
        return 0., "Result file is None"

    expect_rules = rule.get("expect", {})
    unexpect_rules = rule.get("unexpect", {})

    expect_metrics = {k: False for k in expect_rules}
    unexpect_metric = True
    unexpected_keys = []
    
    with open(result) as f:
        for l in f:
            match_: Match[str] = _pref_pattern.match(l.strip())
            if match_ is None:
                continue

            key: str = match_.group("key")
            # value: str = match_.group("val")
            # if value in {"true", "false"}:
            # value = value.title()
            # value: V = eval(value)
            value = json.loads(match_.group("val"))
            if key in expect_rules:
                logger.debug("K: %s, V: %s", key, repr(value))
                expect_metrics[key] = _match_pref(value, expect_rules[key])
            elif key in unexpect_rules:
                if _match_pref(value, unexpect_rules[key]):
                    unexpect_metric = False
                    unexpected_keys.append(key)

    # Generate reason based on results
    reasons = []
    missing_expected = [k for k, v in expect_metrics.items() if not v]
    if missing_expected:
        reasons.append(f"Missing or incorrect expected preferences: {', '.join(missing_expected)}")
    if not unexpect_metric:
        reasons.append(f"Found unexpected preferences: {', '.join(unexpected_keys)}")
    
    score = float(all(expect_metrics.values()) and unexpect_metric)
    reason = "; ".join(reasons) if reasons else "All preferences match expectations"
    
    return score, reason


_value_processor: Callable[[str], str] = lambda val: val.replace("\\\"", "\"").replace("\\\\", "\\")
# _condition_pattern: Pattern[str] = re.compile(r'(?P<type>AND|OR) \((?P<key>[\w ]+),(?P<rel>[\w ' + '\'' + r']+),(?:"(?P<val2>(?:[^"]|\")+)"|(?P<val1>[^)]+))\)')
_condition_pattern: Pattern[str] = re.compile(
    r'\b(?:AND|OR) \((?:[\w ]+),(?:[\w ' + '\'' + r']+),(?:"(?:(?:[^"]|\")+)"|(?:[^)]+))\)|\bALL\b')


def check_thunderbird_filter(result: str, rules: Dict[str, List[Dict[str, str]]]) -> Tuple[float, str]:
    """
    Args:
        result (str): path to filter def file
        rules (Dict[str, List[Dict[str, str]]]): dict like
          {
            "expect": [{key: value}]
            "unexpect": [{key: value}]
          }

    Returns:
        Tuple[float, str]: (score, reason)
    """

    if result is None:
        return 0., "Result file is None"

    # read filter def file
    # a filter:
    # {
    #   "name": "Name",
    #   "enabled": "yes" | "no",
    #   "type": "17",
    #   "action": "Move to folder" | ...,
    #   "actionValue": ...,
    #   "condition": [...]
    # }
    filters: List[Dict[str, Union[str, List[str]]]] = []
    with open(result) as f:
        for l in f:
            if l.startswith("name="):
                filter_: Dict[str, Union[str, List[str]]] = {}
                filter_["name"] = _value_processor(l[6:-2])
            elif l.startswith("enabled="):
                filter_["enabled"] = _value_processor(l[9:-2])
            elif l.startswith("type="):
                filter_["type"] = _value_processor(l[6:-2])
            elif l.startswith("action="):
                filter_["action"] = _value_processor(l[8:-2])
            elif l.startswith("actionValue="):
                filter_["actionValue"] = _value_processor(l[13:-2])
            elif l.startswith("condition="):
                condition_str: str = _value_processor(l[11:-2])
                logger.debug("FILTER CONDITION: %s", condition_str)

                conditions: List[str] = \
                    _condition_pattern.findall(condition_str)
                logger.debug("FILTER CONDITIONS: %s", repr(conditions))

                filter_["condition"] = conditions
                logger.debug("FILTER %s", repr(filter_))
                filters.append(filter_)

    expect_metrics = [False] * len(rules.get("expect", []))
    unexpect_metric = True
    unexpected_filters = []
    
    for flt in filters:
        for i, r in enumerate(rules.get("expect", [])):
            expect_metrics[i] = expect_metrics[i] or _match_record(r, flt)
        if any(_match_record(r, flt) for r in rules.get("unexpect", [])):
            unexpect_metric = False
            unexpected_filters.append(flt.get("name", "unnamed"))
    
    # Generate reason based on results
    reasons = []
    missing_expected_count = expect_metrics.count(False)
    if missing_expected_count > 0:
        reasons.append(f"{missing_expected_count} expected filter(s) not found")
    if not unexpect_metric:
        reasons.append(f"Found unexpected filters: {', '.join(unexpected_filters)}")
    
    score = float(all(expect_metrics) and unexpect_metric)
    reason = "; ".join(reasons) if reasons else "All filters match expectations"
    
    return score, reason


def check_thunderbird_folder(result: Union[str, List[str]], reference: Union[str, List[str]], **kwargs) -> Tuple[float, str]:
    """
    Check the file or file_list that each text file contains all messages in a folder in Thunderbird. Each message is started with `FROM - `.
    **kwargs:
        ignore_status (bool): for comparison, ignore the status (X-Mozilla-Status: 0000) of each message. default: False
        ignore_keys (bool): for comparison, ignore the keys (X-Mozilla-Keys: label) of each message. default: False
        remove_deleted (bool): ignore deleted messages which has status code 0008 or 0009. default: True
        remove_duplicate (bool): remove duplicate messages. default: True
    
    Returns:
        Tuple[float, str]: (score, reason)
    """

    def normalize_msg(msg, options):
        ignore_status = options.get('ignore_status', False)
        ignore_keys = options.get('ignore_keys', False)
        if ignore_status:
            msg = re.sub(r'X-Mozilla-Status\d?:[\s\d]+', '', msg)
        if ignore_keys:
            msg = re.sub(r'(X-Mozilla-Keys:[^\n]*?)\n(MIME-Version)', r'\2', msg)
        return msg.strip()

    def read_thunderbird_folder_file(path: str) -> str:
        with open(path, 'r') as inf:
            data = inf.read().strip()
            messages = []
            for mail in data.split('FROM - '):
                if not mail.strip(): continue
                if kwargs.get('remove_deleted', True) and re.search(r'X-Mozilla-Status: 000[89]', mail): continue
                messages.append('FROM - ' + normalize_msg(mail, kwargs))
            if kwargs.get('remove_duplicate', True):
                messages = list(set(messages))
        return '\n'.join(sorted(messages))

    if type(reference) != list:
        result, reference = [result], [reference]
    
    for i, (pred, gold) in enumerate(zip(result, reference)):
        if pred is None:
            return 0., f"Result file at index {i} is None"
        try:
            mail1 = read_thunderbird_folder_file(pred)
            mail2 = read_thunderbird_folder_file(gold)
            if mail1 != mail2:
                # Count differences
                mail1_set = set(mail1.split('\n'))
                mail2_set = set(mail2.split('\n'))
                missing = len(mail2_set - mail1_set)
                extra = len(mail1_set - mail2_set)
                return 0., f"Folder mismatch at index {i}: {missing} missing messages, {extra} extra messages"
        except Exception as e:
            return 0., f"Error reading folder at index {i}: {str(e)}"
    
    return 1.0, "All folders match perfectly"
