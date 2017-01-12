import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from congress_gov.spiders.congress import CongressSpider as cs
import arrow

process = CrawlerProcess(get_project_settings())

date_format = 'MM/DD/YYYY'
start_date = arrow.get('01/01/2016',date_format)
end_date = arrow.get('01/31/2016', date_format)

for day in arrow.Arrow.range('day', start_date, end_date):
    print(day.format(date_format))

    process.crawl(cs, date=day.format(date_format), item_limit=999)

process.start()
