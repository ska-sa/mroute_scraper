You need to connect to the switch first. Asyncssh will freak out if you don't have trusted keys.

So first
```
ssh monitor@<switch_ip_or_hostname>
#then type "yes"
```

Then 
```
python3 mroute_scraper.py -a <switch_ip_or_hostname>
```
