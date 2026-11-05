"""Expose pytest failures as GitHub check annotations, without extra CI plugins."""

import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    path = Path("build/test-results.xml")
    if not path.is_file():
        return
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        for result in (*case.findall("failure"), *case.findall("error")):
            message = f"{case.get('classname')}.{case.get('name')}\n{result.text or result.get('message', '')}"
            escaped = message[:12000].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error::{escaped}")


if __name__ == "__main__":
    main()
