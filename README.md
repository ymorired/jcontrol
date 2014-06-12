JmeterControll
===============

# Setup
```pip install -r requirements.txt``` will install all required modules.


### Edit ~/.boto file to add credentials
```
[Credentials]
aws_access_key_id = ****
aws_secret_access_key = ****
```

# Run

```
# start instances ( eg. 4 slaves + 1 master)
./jcontrol.py -a run -n 4
# start jmeter-server on slaves
./jcontrol.py -a server
# upload .jmx file and start jmeter
./jcontrol.py -a master -f /path/to/jmeter.jmx
# view report
`./jcontrol.py -a ssh`
cat /var/app/jmeter/bin/out.log
# terminate
./jcontrol.py -a terminate
```
