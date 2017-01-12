import scrapy
import arrow

class CongressSpider(scrapy.spiders.CrawlSpider):
    """Spider for crawling congress.gov"""

    name = 'congress'
    base_URL = 'https://www.congress.gov'

    def __init__(self, date=None, date_format='MM/DD/YYYY'):
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
           yield scrapy.Request(url=url, callback=self.parse_item_page)

    def parse_item_page(self, response):
        text_path = '//div[contains(@class, "txt-box")]/pre[contains(@class, "styled")]/text()'
        text = response.xpath(text_path).extract()