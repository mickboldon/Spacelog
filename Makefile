indexer           = backend/indexer.py
screen_css        = website/static/css/screen.css
source_screen_css = website/static/css/screen/*.css
webserver_ip     ?= 0.0.0.0
webserver_port   ?= 8000

all: reindex productioncss

mcshred: 
	python mcshred/src/MCShred.py /home/assets/transcripts/apollo13/AS13_TEC/0_CLEAN/ 8 transcripts/a13/TEC

reindex: $(indexer)
	rm -rf xappydb
	python -m backend.indexer

productioncss:	$(screen_css)

$(screen_css): $(source_screen_css)
	cssprepare --optimise --extended-syntax \
		$(source_screen_css) > $(screen_css)

devserver:
	python -m website.manage runserver $(webserver_ip):$(webserver_port)

devcss:
	cssprepare --optimise --extended-syntax \
		--pipe $(screen_css) $(source_screen_css)

thumbnails:
	cd website/static/img/missions/a13/; python resize.py
