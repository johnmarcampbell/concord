import json
import re

class RecordParser(object):
    """Object to parse congressional records"""

    def __init__(self, text):
        """Docstring"""
        self.raw_text = text
        self.clean_text = self.remove_page_breaks(self.raw_text)
        self.clean_text = self.remove_midline_returns(self.clean_text)
        self.sections = self.get_sections(self.clean_text)

    def remove_page_breaks(self, raw_text):
        page_break = '\n\[+Pages?.*\]+\n'
        clean_text = re.sub(page_break, '\n', raw_text)
        return clean_text

    # Remove line breaks that cut paragraphs in half
    def remove_midline_returns(self, raw_text):
        mid_line_returns = '\n(\w)'
        clean_text = re.sub(mid_line_returns, '\\1', raw_text)
        return clean_text

    # Split document around blank lines
    # to get multi-paragraph sections
    def get_sections(self, text):
        sections = []
        paragraph = ''
        for line in text.splitlines():
            paragraph += line
            if line == '':
                sections.append(paragraph)
                paragraph = ''

        # Throw away empty sections
        sections = [val for val in sections if val != '']
        return sections

    # Look at the beginning of a section to see if
    # it corresponds to someone speaking. If it does,
    # figure out who
    def get_speaker(self, section):
        titles = '(?:The|Mr\.|Mrs\.|Ms\.)'
        caps_name_or_title = '([A-Z][A-Za-z ]+)'
        paren_name = '(?:\(([A-Za-z. ]+)\))?'
        speaker_string = '  {} {}{}\.'.format(titles, caps_name_or_title, paren_name)

        speaker_match = re.match(speaker_string, section)
        if self.match_is_good(speaker_match):
            if speaker_match.group(2):
                speaker = speaker_match.group(2).lower()
            else: 
                speaker = speaker_match.group(1).lower()
        else:
            speaker = None

        # Check for 'Mr. Foo of Bar' and get 'Foo'
        if speaker:
            of_match = re.search('([a-z]+) of ', speaker)
            if of_match:
                speaker = of_match.group(1)

        # Check for 'Mr. Foo' and get 'Foo'
        if speaker:
            title_match = re.match('(?:mr\.|mrs\.|ms\.) (\w+)', speaker)
            if title_match:
                speaker = title_match.group(1)

        return speaker

    # After getting a speaker match from
    # get_speaker() (below), double-check
    # a few things to remove false positives
    def match_is_good(self, match):
        ignore_phrases = ['chair recognizes', 'during the vote', 'committee resumed']
        word_limit = 7
        match_is_good = True

        if match:
            for text in match.groups():
                if text and (len(text.split()) > word_limit):
                    match_is_good = False
            for phrase in ignore_phrases:
                if text and re.match(phrase, text.lower()):
                    match_is_good = False
        else:
            match_is_good = False


        return match_is_good
