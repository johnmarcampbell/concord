# -*- coding: utf-8 -*-

# Define here the models for your scraped items
#
# See documentation in:
# http://doc.scrapy.org/en/latest/topics/items.html

import scrapy


class CongressItem(scrapy.Item):
    url = scrapy.Field()

    title = scrapy.Field()
    date = scrapy.Field()

    congress = scrapy.Field()
    session = scrapy.Field()

    issue = scrapy.Field()
    volume = scrapy.Field()

    start_page = scrapy.Field()
    end_page = scrapy.Field()

    text = scrapy.Field()
