import json
import re

class RecordParser(object):
    """Object to parse congressional records"""

    def __init__(self, text):
        """Docstring"""
        self.text = text

    def remove_page_breaks(self):
        page_break = '\n\[+Pages?.*\]+\n'
        clean_text = re.sub(page_break, '\n', self.text)
        self.text = clean_text

