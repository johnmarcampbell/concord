import json
# -*- coding: utf-8 -*-

# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: http://doc.scrapy.org/en/latest/topics/item-pipeline.html


class CongressGovPipeline(object):
    def process_item(self, item, spider):
        with open('output.json','a') as f:
            line = json.dumps(dict(item)) + '\n'
            f.write(line)

        return item
