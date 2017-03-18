class Appointment(object):
    """This object represents one appointment to congress """

    def __init__(self, position='', party='', state='', congress='', 
        begin_year='', end_year=''):
        self.position = position
        self.party = party
        self.state = state
        self.congress = congress
        self.begin_year = begin_year
        self.end_year = end_year,
        """Set some values"""
    
