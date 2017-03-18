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
    
