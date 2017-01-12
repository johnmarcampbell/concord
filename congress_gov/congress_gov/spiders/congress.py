import scrapy
from congress_gov.items import CongressItem
import arrow
import re

class CongressSpider(scrapy.spiders.CrawlSpider):
    """Spider for crawling congress.gov"""

    name = 'congress'
    base_URL = 'https://www.congress.gov'

    def __init__(self, **kwargs):
        """Congressional Record Spider
        
        Arguments:
        date -- A string representing the date of the records to
        up. If none is provided, the spider will automatically try to
        look up *yesterday's* records

        date_format -- a date format for specifying the date_string. 
        See http://crsmithdev.com/arrow/#tokens for more info.

        Example command: scrapy crawl congress -a date='10/13/2016'
        
        """
        self.set = self.get_settings(**kwargs)

        if self.set['start_date'] is None:
            first = arrow.utcnow().replace(days=-1)
        else:
            first = arrow.get(self.set['start_date'], self.set['date_format'])
        if self.set['end_date'] is None:
            last = arrow.utcnow().replace(days=-1)
        else:
            last = arrow.get(self.set['end_date'], self.set['date_format'])

        self.dates = arrow.Arrow.range('day', first, last)

        self.item_count = 0
        self.item_limit = int(self.set['item_limit'])

    def start_requests(self):

        for date in self.dates:
            date_URL = '{}/{}/{}'.format(date.year, date.month, date.day)
            for section in self.set['sections']:
                url_mask = '{}/congressional-record/{}/{}/'
                url = url_mask.format(self.base_URL, date_URL, section)
                yield scrapy.Request(url=url, callback=self.parse_landing_page)


    def parse_landing_page(self, response):
       item_path = '//table/tbody/tr/td/a[contains(@href, "article")]/@href'

       for item_URL in response.xpath(item_path).extract():
           url = '{}/{}'.format(self.base_URL, item_URL)
           if(self.item_count < self.item_limit):
               yield scrapy.Request(url=url, callback=self.parse_item_page)
               self.item_count += 1


    def parse_item_page(self, response):
        text_path = '//div[contains(@class, "txt-box")]/pre[contains(@class, "styled")]/text()'
        date_path = '//div[contains(@class, "cr-issue")]/h3/text()'
        blurb_path = '//div[contains(@class, "cr-issue")]/h4/text()'
        title_path = '//div[contains(@class, "wrapper_std")]/h2/text()'

        text = response.xpath(text_path).extract()
        date = response.xpath(date_path).extract_first()
        blurb = response.xpath(blurb_path).extract()
        title = response.xpath(title_path).extract_first()
        nth_congress_session = blurb[0]
        issue_vol = blurb[1]

        def get_nth_congress_session(the_string):
            regex_string = '([0-9]+).* Congress, ([0-9]+).* Session'

            match = re.search(regex_string, the_string)

            if match:
                (congress, session) = match.groups()
            else:
                (congress, session) = (None, None)

            return (congress, session)

        def get_volume_number(the_string):
            regex_string = '([0-9]+).* Congress, ([0-9]+).* Session'
            regex_string = 'Issue: Vol\. ([0-9]+), No\. ([0-9]+)'

            match = re.search(regex_string, the_string)

            if match:
                (volume, number) = match.groups()
            else:
                (volume, number) = (None, None)

            return (volume, number)


        (congress, session) = get_nth_congress_session(nth_congress_session)
        (volume, number) = get_volume_number(issue_vol)

        item = CongressItem(
            url=response.url,
            title=title,
            date=date,
            congress=congress,
            session=session,
            number=number,
            volume=volume,
            text=text
        )

        return item

    def get_settings(self, **kwargs):
        '''Turn a set of keywords arguments into a dictionary of settings
        '''
        # Dictionary with default values
        settings = dict(
            item_limit=10,
            start_date=None,
            end_date=None,
            date_format='MM/DD/YYYY',
            sections=['senate-section', 'house-section', 
                        'extensions-of-remarks-section'],
        )

        badargs = set(kwargs) - set(settings)

        if badargs:
            err = 'CongressSpider() got unexpected keyword arguments: {}.'
            raise TypeError( err.format(list(badargs)) )
        else:
            settings.update(kwargs)

        print(settings)
        return settings
