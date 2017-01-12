import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from congress_gov.spiders.congress import CongressSpider as cs
import arrow
import logging

settings = get_project_settings()
settings['LOG_FILE'] = 'log.txt'

process = CrawlerProcess(settings)

start = '01/01/2016'
end = '01/10/2016'
limit = 999

process.crawl(cs, start_date=start, end_date=end, item_limit=limit)
process.start()
