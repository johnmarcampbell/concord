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

    # Remove line breaks that cut paragraphs in half
    def remove_midline_returns(self):
        mid_line_returns = '\n(\w)'
        clean_text = re.sub(mid_line_returns, '\\1', self.text)
        self.text = clean_text

    # Split document around blank lines
    # to get multi-paragraph sections
    def get_sections(self):
        sections = []
        paragraph = ''
        for line in self.text.splitlines():
            paragraph += line
            if line == '':
                sections.append(paragraph)
                paragraph = ''

        # Throw away empty sections
        sections = [val for val in sections if val != '']
        return sections
