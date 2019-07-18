import scrapy
from congress_gov.items import CongressItem
import arrow
import re

class CongressSpider(scrapy.spiders.CrawlSpider):
    """Spider for crawling congress.gov
    
    Keyword arguments:
    item_limit -- A limit on the number of items to download.

    start_date -- Spider begins parsing records at this date. If none
    is provided, this will automatically set to *yesterday's* date

    end_date -- Spider stops parsing records after this date. If none
    is provided, this will automatically set to *yesterday's* date

    date_format -- a date format for specifying the date_string.  See
    http://crsmithdev.com/arrow/#tokens for more info.

    sections -- A list of sections to crawl. Must be selected from:
        [senate-section, house-section, extensions-of-remarks-section]

    Example command: scrapy crawl congress -a date='10/13/2016'
        
    """

    name = 'congress'
    base_URL = 'https://www.congress.gov'

    def __init__(self, **kwargs):
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
            date_URL = '{}/{}/{}'.format(
                date.year, str(date.month).zfill(2), str(date.day).zfill(2))
            url_mask = '{}/congressional-record/{}'
            url = url_mask.format(self.base_URL, date_URL)
            yield scrapy.Request(url=url, callback=self.parse_daily_page)

    def parse_daily_page(self, response):
        """Parse a page corresponding to a given date"""
        url_mask = response.url[len('https://www.congress.gov'):]
        for section in self.set['sections']:
            partial_url = '{}/{}'.format(url_mask, section)
            if partial_url in response.xpath('//a/@href').extract():
                url = 'https://www.congress.gov{}'.format(partial_url)
                yield scrapy.Request(url=url, callback=self.parse_section_page)

    def parse_section_page(self, response):
        """Parse a house/senate/remarks page looking for links to item pages"""
        item_path = '//table/tbody/tr/td/a[contains(@href, "article")]/@href'

        for item_URL in response.xpath(item_path).extract():
           url = '{}{}'.format(self.base_URL, item_URL)
           if(self.item_count < self.item_limit):
               yield scrapy.Request(url=url, callback=self.parse_item_page)
               self.item_count += 1


    def parse_item_page(self, response):
        """Parse and item page to scrape individual CongressItem's"""
        raw_text_path = '//div[contains(@class, "txt-box")]/pre[contains(@class, "styled")]'
        linked_text_path = '//div[contains(@class, "txt-box")]/pre[contains(@class, "styled")]/a/text()'
        date_path = '//div[contains(@class, "cr-issue")]/h3/text()'
        blurb_path = '//div[contains(@class, "cr-issue")]/h4/text()'
        title_path = '//div[contains(@class, "wrapper_std")]/h2/text()'

        linked_text = response.xpath(linked_text_path).extract()
        raw_date = response.xpath(date_path).extract_first()
        date = arrow.get(raw_date, 'MMMM D, YYYY ').datetime
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

        def get_volume_issue(the_string):
            regex_string = '([0-9]+).* Congress, ([0-9]+).* Session'
            regex_string = 'Issue: Vol\. ([0-9]+), No\. ([0-9]+)'

            match = re.search(regex_string, the_string)

            if match:
                (volume, issue) = match.groups()
            else:
                (volume, issue) = (None, None)

            return (volume, issue)


        (congress, session) = get_nth_congress_session(nth_congress_session)
        (volume, issue) = get_volume_issue(issue_vol)

        def get_page_range(linked_text):
            regex_string = 'Page[s]* ([A-Z][0-9]+)\-?([A-Z][0-9]+)?'
            for text in linked_text:
                match = re.search(regex_string, text)
                if match:
                    (first, last) = match.groups()
                    if last is None:
                        last = first
                    return (first, last)



        (start, end) = get_page_range(linked_text)

        def get_clean_text(raw_text):
            clean = ''
            for text in raw_text.xpath('.//text()').extract():
                clean += text

            return clean

        text = get_clean_text(response.xpath(raw_text_path))

        item = CongressItem(
            url=response.url,
            title=title,
            date=date,
            congress=congress,
            session=session,
            issue=issue,
            volume=volume,
            start_page=start,
            end_page=end,
            text=text
        )

        return item

    def get_settings(self, **kwargs):
        """Turn a set of keywords arguments into a dictionary of settings """
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

        return settings
