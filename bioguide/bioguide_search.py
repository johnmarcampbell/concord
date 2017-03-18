import requests
from bs4 import BeautifulSoup

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

        for row in results_table.findAll('tr'):
            # Using findAll('td') skips the table headers, which are
            # referenced by 'th'
            cells = row.findAll('td')
            if cells:
                print(cells[0].text)
        

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
