class BioguideSearch(object):
    """An object that will perform searches on the Biographical
    Directory of the US Congress"""

    def __init__(self, **kwargs):
        """Set some defaults"""
        self.settings = self.get_settings(**kwargs)

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
