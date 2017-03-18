import requests

class BioguideSearch(object):
    """An object that will perform searches on the Biographical
    Directory of the US Congress"""

    def __init__(self, **kwargs):
        """Set some defaults"""
        self.settings = self.get_settings(**kwargs)

    def get_payload(self):
        """This function goes through the self.settings dictionary and
        picks out the settings that correspond to a payload of search
        parameters"""

        payload = dict(
            first_name=self.settings['first_name'],
            last_name=self.settings['last_name'],
            position=self.settings['position'],
            state=self.settings['state'],
            party=self.settings['party'],
            year_or_congress=self.settings['year_or_congress']
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
            err = 'BiogiodeSearch() got unexpected keyword arguments: {}.'
            raise TypeError( err.format(list(badargs)) )
        else:
            settings.update(kwargs)

        return settings
