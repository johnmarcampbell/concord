import json
import re

def clean_record(text, 
                 page_breaks=True,
                 midline_returns=True,
                 time_marks=True):
    """This function wraps several others and allows them to be turned 
    on or off using keyword arguments"""
    clean_text = text
    if(page_breaks):
        clean_text = remove_page_breaks(clean_text)
    if(midline_returns):
        clean_text = remove_midline_returns(clean_text)
    if(time_marks):
        clean_text = remove_time_marks(clean_text)
        
    return clean_text

def remove_page_breaks(raw_text):
    page_break = '\n\[+Pages?.*\]+\n'
    clean_text = re.sub(page_break, '\n', raw_text)
    return clean_text

# Remove line breaks that cut paragraphs in half
def remove_midline_returns(raw_text):
    mid_line_returns = '\n(\w)'
    clean_text = re.sub(mid_line_returns, '\\1', raw_text)
    return clean_text

def remove_time_marks(raw_text):
    time_marks = '\{time\}.*\n'
    clean_text = re.sub(time_marks, '', raw_text)
    return clean_text
    

# Split document around blank lines
# to get multi-paragraph sections
def get_sections(text):
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

# After getting a speaker match from
# get_speaker() (below), double-check
# a few things to remove false positives
def match_is_good(match):
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

# Look at the beginning of a section to see if
# it corresponds to someone speaking. If it does,
# figure out who
def get_speaker(section):
    titles = '(?:The|Mr\.|Mrs\.|Ms\.)'
    caps_name_or_title = '([A-Z][A-Za-z ]+)'
    paren_name = '(?:\(([A-Za-z. ]+)\))?'
    speaker_string = '  {} {}{}\.'.format(titles, caps_name_or_title, paren_name)

    speaker_match = re.match(speaker_string, section)
    if match_is_good(speaker_match):
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
