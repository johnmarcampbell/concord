# Concord: Scraping the daily congressional record from [congress.gov](https://www.congress.gov)  

## Requirements  
- Python 3.3+  
- [Scrapy 1.3.0](https://doc.scrapy.org/en/1.3/index.html)  

## What is the *Congressional Record*?  
From the [legistlative glossary](https://www.congress.gov/help/legislative-glossary):  

> The Congressional Record is the official record of the proceedings and 
> debates of the U.S. Congress. For every day Congress is in session, an 
> issue of the Congressional Record is printed by the Government Publishing  
> Office. Each issue summarizes the day's floor and committee actions and  
> records all remarks delivered in the House and Senate.  

## Data Spec  
The `congress` spider returns a separate item for each proceeding in the [congressional record](https://www.congress.gov/congressional-record) (hereafer: "the record"). Each item contains the following fields:  

- `url`: The URL of the page where the proceeding was found  
- `title`: The title of the proceeding  
- `date`: The date the proceeding  
- `congress`: Which 2-year congress had the proceeding (E.g., the 114th congress, the 115th congress, etc)  
- `session`: Which session of congress had the proceeding  
- `issue`: Which issue of of the record has this proceeding (There is one issue for each day that congress meets)  
- `volume`: Which volume of the record has this proceeding  
- `start_page`: The page of the record where this proceeding begins  
- `end_page`: The page of the record where this proceeding ends  
- `text`: The text of the proceeding  

## Running Concord  

- First clone the repo and install dependencies:  
```shell  
git clone https://github.com/johnmarcampbell/concord  
cd concord  
[set up a virtual environment here if you like]  
pip install -r requirements.txt  
```  

- Concord can be run from the command line or using the included `runSpider.py` script.  
```shell  
congress_gov  
scrapy crawl congress # command line  
python runSpider.py # script  
```  

The `congress` spider can take the following arguments:  
- `item_limit`: A limit on the number of items to download.  
` `start_date`: Spider begins parsing records at this date. If none is provided, this will automatically set to *yesterday's* date  
- `end_date`: Spider stops parsing records after this date. If none is provided, this will automatically set to *yesterday's* date  
- `date_format`: A date format for specifying the date_string.  See [`arrow` documentation](http://crsmithdev.com/arrow/#tokens) for more info. 

- `sections`: A list of sections to crawl. Must be selected from `senate-section`, `house-section`, or `extensions-of-remarks-section`  

These arguments may be specifed in the `runSpider.py` script, or on the command line with:  

```shell  
scrapy crawl congress -a argument1=value1 -a argument2=value2 -a argument3=value3 ...  
```  
