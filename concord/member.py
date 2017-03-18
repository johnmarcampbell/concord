class Member(object):
    """This object represents one member of congress"""

    def __init__(self, last_name='', first_name='', middle_name='',
        bioguide_id='', birth_year='', death_year='',  appointments=[]):
        self.last_name = last_name
        self.first_name = first_name
        self.middle_name = middle_name
        self.bioguide_id = bioguide_id
        self.birth_year = birth_year
        self.death_year = death_year
        self.appointments = appointments
        """Set some values"""

    def __str__(self):
        """String representation of a Member object"""
        # Set middle name to empty string if there isn't one
        if self.middle_name:
            m = ' {}'.format(self.middle_name)
        else:
            m = ''
        
        mask = '{}, {}{} [{}] - ({}-{})'
        return mask.format(self.last_name, self.first_name, m, self.bioguide_id,
                           self.birth_year, self.death_year)

    
