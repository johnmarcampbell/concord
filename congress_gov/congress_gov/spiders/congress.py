import scrapy
import arrow

class CongressSpider(scrapy.spiders.CrawlSpider):
    """Spider for crawling congress.gov"""

    name = 'congress'
    base_URL = 'https://www.congress.gov'

    def __init__(self, date=None, date_format='MM/DD/YYYY', item_limit=1):
        """Congressional Record Spider
        
        Arguments:
        date -- A string representing the date of the records to
        up. If none is provided, the spider will automatically try to
        look up *yesterday's* records

        date_format -- a date format for specifying the date_string. 
        See http://crsmithdev.com/arrow/#tokens for more info.

        Example command: scrapy crawl congress -a date='10/13/2016'
        
        """
        if date is None:
            a = arrow.utcnow().replace(days=-1)
        else:
            a = arrow.get(date, date_format)

        self.url_date = '{}/{}/{}'.format(a.year, a.month, a.day)

        self.item_count = 0
        self.item_limit = int(item_limit)

    def start_requests(self):
        sections = ['senate-section',
                    'house-section', 
                    'extensions-of-remarks-section'
                    ]

        for section in sections:
            url_mask = '{}/congressional-record/{}/{}/'
            url = url_mask.format(self.base_URL, self.url_date, section)
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

        print('-----')
        print(title)
        print(date)
        print(nth_congress_session)
        print(issue_vol)
        print('-----')
