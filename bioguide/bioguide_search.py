import requests
from bs4 import BeautifulSoup
import re

class BioguideSearch(object):
    """An object that will perform searches on the Biographical
    Directory of the US Congress"""

    def __init__(self, **kwargs):
        """Set some defaults"""
        self.settings = self.get_settings(**kwargs)

    def search(self):
        """Function that performs the bioguide search"""
        self.request = requests.get(
            url=self.settings['url'], data=self.get_payload())
        self.soup = BeautifulSoup(self.request.text, 'lxml')
        self.parse_results()

    def parse_results(self):
        """Parse the results of the bioguide search"""
        results_table = self.soup.findAll('table')[1]

        def get_bioguide_id(cell):
            """Take a cell with a link to a bioguide page, and return the ID"""
            url_base = 'http://bioguide.congress.gov/scripts/biodisplay.pl\?index=' 
            bioguide_id_mask = '([A-Z][0-9]{6})'
            regex_search_string = '{}{}'.format(url_base, bioguide_id_mask)

            link = cell.find('a').get('href') 
            match = re.search(regex_search_string, link)
            if(match):
                return match.group(1)
            else:
                return None

        def get_name(cell):
            """Take a cell with name in it, and return the first and last name"""
            first = '([A-Za-z]+)' 
            middle = '([A-Za-z]+)'
            last = '([A-Z]+)'

            # Middle name is optional, don't capture space between 
            # first and middle name
            regex_search_string = '{}, {}(?: {})?'.format(last, first, middle)
            
            match = re.search(regex_search_string, cell.text)
            if(match):
                return match.groups()
            else:
                return (None, None, None)

        def get_birth_death(cell):
            """Take a cell with birth/death years, and return the years"""
            regex_search_string = '([0-9]{4})-([0-9]{4})?'
            match = re.search(regex_search_string, cell.text)
            if(match):
                return match.groups()
            else:
                return (None, None)

        def get_congress_and_year(cell):
            """Take a cell with congress info and parse it"""
            congress = '([0-9]{1,3})'
            years = '([0-9]{4})-([0-9]{4})'
            regex_search_string = r'{}(?:\({}\))?'.format(congress, years)
            match = re.search(regex_search_string, cell.text)
            if(match):
                return match.groups()
            else:
                return (None, None, None)
            

        for row in results_table.findAll('tr'):
            # Using findAll('td') skips the table headers, which are
            # referenced by 'th'
            cells = row.findAll('td')
            if cells:
                position = cells[2].text
                party = cells[3].text
                state = cells[4].text
                (congress, begin_year, end_year) = get_congress_and_year(cells[5])
                appointment = (position, party, state, congress, begin_year, end_year)

                # Check to see if this row starts a new member
                (last, first, middle) = get_name(cells[0])
                if last:
                    bioguide_id = get_bioguide_id(cells[0])
                    (birth_year, death_year) = get_birth_death(cells[1])
                    member = (last, first, middle, bioguide_id, 
                        birth_year, death_year)
                    print('{}, {} {} [{}] ({}-{})'.format(*member))
                print('\t{} : {} : {} : {},{}-{}'.format(*appointment))
        

    def get_payload(self):
        """This function goes through the self.settings dictionary and
        picks out the settings that correspond to a payload of search
        parameters"""

        payload = dict(
            firstname=self.settings['first_name'],
            lastname=self.settings['last_name'],
            position=self.settings['position'],
            state=self.settings['state'],
            party=self.settings['party'],
            congress=self.settings['year_or_congress']
            )

        return payload
        
    def get_settings(self, **kwargs):
        """Turn a set of keyword arguments into a dictionary of settings """
        # Dictionary with default values
        settings = dict(
            first_name='',
            last_name='',
            position='',
            state='',
            party='',
            year_or_congress='',
            url='http://bioguide.congress.gov/biosearch/biosearch1.asp'
        )

        badargs = set(kwargs) - set(settings)

        if badargs:
            err = 'BioguideSearch() got unexpected keyword arguments: {}.'
            raise TypeError( err.format(list(badargs)) )
        else:
            settings.update(kwargs)

        return settings
