import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from congress_gov.spiders.congress import CongressSpider as cs

process = CrawlerProcess(get_project_settings())

process.crawl(cs, date='10/13/2016', item_limit=999)
process.start()
