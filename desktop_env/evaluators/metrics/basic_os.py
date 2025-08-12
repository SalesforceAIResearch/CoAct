def check_gnome_favorite_apps(apps_str: str, rule):
    # parse the string like "['thunderbird.desktop', 'vim.desktop', 'google-chrome.desktop']"
    # to a list of strings
    apps = eval(apps_str)

    expected_apps = rule["expected"]

    if len(apps) != len(expected_apps):
        return 0, f"Number of apps mismatch: expected {len(expected_apps)}, got {len(apps)}"

    if set(apps) == set(expected_apps):
        return 1, "All favorite apps match expected apps"
    else:
        missing = set(expected_apps) - set(apps)
        extra = set(apps) - set(expected_apps)
        reason = []
        if missing:
            reason.append(f"Missing apps: {missing}")
        if extra:
            reason.append(f"Extra apps: {extra}")
        return 0, "; ".join(reason)


def is_utc_0(timedatectl_output):
    """
    Format as:
    Local time: Thu 2024-01-25 12:56:06 WET
           Universal time: Thu 2024-01-25 12:56:06 UTC
                 RTC time: Thu 2024-01-25 12:56:05
                Time zone: Atlantic/Faroe (WET, +0000)
System clock synchronized: yes
              NTP service: inactive
          RTC in local TZ: no
    """

    utc_line = timedatectl_output.split("\n")[3]

    if utc_line.endswith("+0000)"):
        return 1, "Timezone is UTC+0"
    else:
        # Extract the actual timezone offset
        import re
        offset_match = re.search(r'([+-]\d{4})\)', utc_line)
        if offset_match:
            offset = offset_match.group(1)
            return 0, f"Timezone is not UTC+0, current offset is {offset}"
        else:
            return 0, "Timezone is not UTC+0"


def check_text_enlarged(scaling_factor_str):
    scaling_factor = float(scaling_factor_str)
    if scaling_factor > 1.0:
        return 1, f"Text is enlarged with scaling factor {scaling_factor}"
    else:
        return 0, f"Text is not enlarged, scaling factor is {scaling_factor}"


def check_moved_jpgs(directory_list, rule):
    expected_jpgs = rule["expected"]
    moved_jpgs = [node['name'] for node in directory_list['children']]

    if len(moved_jpgs) != len(expected_jpgs):
        return 0, f"Number of JPG files mismatch: expected {len(expected_jpgs)}, got {len(moved_jpgs)}"

    if set(moved_jpgs) == set(expected_jpgs):
        return 1, "All JPG files successfully moved"
    else:
        missing = set(expected_jpgs) - set(moved_jpgs)
        extra = set(moved_jpgs) - set(expected_jpgs)
        reason = []
        if missing:
            reason.append(f"Missing files: {missing}")
        if extra:
            reason.append(f"Extra files: {extra}")
        return 0, "; ".join(reason)


def is_in_vm_clickboard(config, terminal_output):
    print("terminal_output: ")
    print(terminal_output)
    print("config: ")
    print(config)
    expected_results = config["expected"]
    # check if terminal_output has expected results
    if not isinstance(expected_results, list):
        if expected_results in terminal_output:
            return 1, f"Found expected content '{expected_results}' in clipboard"
        else:
            return 0, f"Expected content '{expected_results}' not found in clipboard"
    else:
        missing = [result for result in expected_results if result not in terminal_output]
        if not missing:
            return 1, f"All expected results found in clipboard: {expected_results}"
        else:
            return 0, f"Missing expected results in clipboard: {missing}"
