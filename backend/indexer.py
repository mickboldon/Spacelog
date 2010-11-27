import os
import redis
import xappy

from backend.parser import TranscriptParser, MetaParser
from backend.api import Act, Character, Glossary

search_db = xappy.IndexerConnection(
    os.path.join(os.path.dirname(__file__), '..', 'xappydb'),
)

class TranscriptIndexer(object):
    """
    Parses a file and indexes it.
    """

    LINES_PER_PAGE = 20

    def __init__(self, redis_conn, mission_name, transcript_name, parser):
        self.redis_conn = redis_conn
        self.mission_name = mission_name
        self.transcript_name = transcript_name
        self.parser = parser

        search_db.add_field_action(
            "mission",
            xappy.FieldActions.INDEX_EXACT,
            # search_by_default=False,
            # allow_field_specific=False,
        )
        # don't think we need STORE_CONTENT actions any more
        search_db.add_field_action(
            "speaker",
            xappy.FieldActions.STORE_CONTENT,
        )
        # Can't use facetting unless Xapian supports it
        # can't be bothered to check this (xappy._checkxapian.missing_features['facets']==1)
        #
        # search_db.add_field_action(
        #     "speaker",
        #     xappy.FieldActions.FACET,
        #     type='string',
        # )
        search_db.add_field_action(
            "speaker",
            xappy.FieldActions.INDEX_FREETEXT,
            weight=1,
            language='en',
            search_by_default=True,
            allow_field_specific=True,
        )
        search_db.add_field_action(
            "text",
            xappy.FieldActions.STORE_CONTENT,
        )
        search_db.add_field_action(
            "text",
            xappy.FieldActions.INDEX_FREETEXT,
            weight=1,
            language='en',
            search_by_default=True,
            allow_field_specific=False,
            spell=True,
        )
        search_db.add_field_action(
            "weight",
            xappy.FieldActions.SORTABLE,
            type='float',
        )
        # Add names as synonyms for speaker identifiers
        characters = Character.Query(self.redis_conn, self.mission_name).items()
        for character in characters:
            for name in [character.name, character.short_name]:
                for bit in name.split():
                    search_db.add_synonym(bit, character.identifier)
                    search_db.add_synonym(bit, character.identifier, field='speaker')

    def add_to_search_index(self, mission, id, lines, weight=1):
        """
        Take some text and a set of speakers (also text) and add a document
        to the search index, with the id stuffed in the document data.
        """
        doc = xappy.UnprocessedDocument()
        doc.fields.append(xappy.Field("mission", mission))
        doc.fields.append(xappy.Field("weight", weight))
        for line in lines:
            doc.fields.append(xappy.Field("text", line['text']))
            doc.fields.append(xappy.Field("speaker", line['speaker']))
        doc.id = id
        try:
            search_db.add(search_db.process(doc))
        except xappy.errors.IndexerError:
            print "umm, error"
            print id, text, speakers
            raise

    def index(self):
        current_labels = {}
        current_transcript_page = None
        current_page = 1
        current_page_lines = 0
        last_act = None
        previous_log_line_id = None
        launch_time = int(self.redis_conn.get("launch_time:%s" % self.mission_name))
        acts = list(Act.Query(self.redis_conn, self.mission_name))
        glossary_items = dict([
            (item.identifier.lower(), item) for item in
            Glossary.Query(self.redis_conn, self.mission_name)
        ])
        for chunk in self.parser.get_chunks():
            timestamp = chunk['timestamp']
            log_line_id = "%s:%i" % (self.transcript_name, timestamp)
            # See if there's transcript page info, and update it if so
            if chunk['meta'].get('_page', 0):
                current_transcript_page = int(chunk["meta"]['_page'])
            if current_transcript_page:
                self.redis_conn.set("log_line:%s:page" % log_line_id, current_transcript_page)
            # Look up the act
            for act in acts:
                if act.includes(timestamp):
                    break
            else:
                raise RuntimeError("No act for timestamp %i" % timestamp)
            # If we've filled up the current page, go to a new one
            if current_page_lines >= self.LINES_PER_PAGE or (last_act is not None and last_act != act):
                current_page += 1
                current_page_lines = 0
            last_act = act
            # First, create a record with some useful information
            self.redis_conn.hmset(
                "log_line:%s:info" % log_line_id,
                {
                    "offset": chunk['offset'],
                    "page": current_page,
                    "transcript_page": current_transcript_page,
                    "act": act.number,
                    "utc_time": launch_time + timestamp,
                }
            )
            # Create the doubly-linked list structure
            if previous_log_line_id:
                self.redis_conn.hset(
                    "log_line:%s:info" % log_line_id,
                    "previous",
                    previous_log_line_id,
                )
                self.redis_conn.hset(
                    "log_line:%s:info" % previous_log_line_id,
                    "next",
                    log_line_id,
                )
            previous_log_line_id = log_line_id
            # Also store the text
            text = ""
            for line in chunk['lines']:
                self.redis_conn.rpush(
                    "log_line:%s:lines" % log_line_id,
                    "%(speaker)s: %(text)s" % line,
                )
                text += "%s %s" % (line['speaker'], line['text'])
            # Store any images
            for i, image in enumerate(chunk['meta'].get("_images", [])):
                # Make the image id
                image_id = "%s:%s" % (log_line_id, i)
                # Push it onto the images list
                self.redis_conn.rpush(
                    "log_line:%s:images" % log_line_id,
                    image_id,
                )
                # Store the image data
                self.redis_conn.hmset(
                    "image:%s" % image_id,
                    image,
                )
            # Add that logline ID for the people involved
            speakers = set([ line['speaker'] for line in chunk['lines'] ])
            for speaker in speakers:
                self.redis_conn.sadd("speaker:%s" % speaker, log_line_id)
            # Add it to the index for this page
            self.redis_conn.rpush("page:%s:%i" % (self.transcript_name, current_page), log_line_id)
            # Add it into the transcript and everything sets
            self.redis_conn.zadd("log_lines:%s" % self.mission_name, log_line_id, chunk['timestamp'])
            self.redis_conn.zadd("transcript:%s" % self.transcript_name, log_line_id, chunk['timestamp'])
            # Read the new labels into current_labels
            if '_labels' in chunk['meta']:
                for label, endpoint in chunk['meta']['_labels'].items():
                    if endpoint is not None and label not in current_labels:
                        current_labels[label] = endpoint
                    elif label in current_labels:
                        current_labels[label] = max(
                            current_labels[label],
                            endpoint
                        )
                    elif endpoint is None:
                        self.redis_conn.sadd("label:%s" % label, log_line_id)
            # Expire any old labels
            for label, endpoint in current_labels.items():
                if endpoint < chunk['timestamp']:
                    del current_labels[label]
            # Apply any surviving labels
            for label in current_labels:
                self.redis_conn.sadd("label:%s" % label, log_line_id)
            # And add this logline to search index
            if len(current_labels):
                weight = 3 # magic!
            else:
                weight = 1
            self.add_to_search_index(
                mission=self.mission_name,
                id=log_line_id,
                lines = chunk['lines'],
                weight=weight,
            )
            # For any mentioned glossary terms, add to them.
            for word in text.split():
                word = word.strip(",;-:'\"").lower()
                if word in glossary_items:
                    glossary_item = glossary_items[word]
                    self.redis_conn.hincrby(
                        "glossary:%s" % glossary_item.id,
                        "times_mentioned",
                        1,
                    )
            # Increment the number of log lines we've done
            current_page_lines += len(chunk['lines'])


class MetaIndexer(object):
    """
    Takes a mission folder and reads and indexes its meta information.
    """

    def __init__(self, redis_conn, mission_name, parser):
        self.redis_conn = redis_conn
        self.parser = parser
        self.mission_name = mission_name

    def index(self):
        meta = self.parser.get_meta()

        self.redis_conn.set("launch_time:%s" % self.mission_name, meta['utc_launch_time'])

        self.index_narative_elements(meta)
        self.index_glossary(meta)
        self.index_characters(meta)

    def index_narative_elements(self, meta):
        "Stores acts and key scenes in redis"
        for noun in ('act', 'key_scene'):
            for i, data in enumerate(meta.get('%ss' % noun, [])):
                key = "%s:%s:%i" % (noun, self.mission_name, i)
                self.redis_conn.rpush(
                    "%ss:%s" % (noun, self.mission_name),
                    "%s:%i" % (self.mission_name, i),
                )

                data['start'], data['end'] = data['range']
                del data['range']

                self.redis_conn.hmset(key, data)

    def index_characters(self, meta):
        "Stores character information in redis"
        for identifier, data in meta['characters'].items():
            mission_key   = "characters:%s" % self.mission_name
            character_key = "%s:%s" % (mission_key, identifier)
            
            self.redis_conn.rpush(mission_key, identifier)
            self.redis_conn.rpush(
                '%s:%s' % (mission_key, data['role']),
                identifier
            )
            
            # Push stats as a list so it's in-order later
            for stat in data.get('stats', []):
                self.redis_conn.rpush(
                    '%s:stats' % character_key, 
                    "%s:%s" % (stat['value'], stat['text'])
                )
            if 'stats' in data:
                del data['stats']
            
            self.redis_conn.hmset(character_key, data)

    def index_glossary(self, meta):
        "Stores glossary information in redis"
        for identifier, data in meta['glossary'].items():
            character_key = "%s:%s" % (self.mission_name, identifier)
            
            # Add the ID to the list for this mission
            self.redis_conn.rpush("glossary:%s" % self.mission_name, identifier)

            # Extract the links from the data
            links = data.get('links', [])
            if "links" in data:
                del data['links']
            
            data['abbr'] = identifier
            data['times_mentioned'] = 0
            
            # Store the main data in a hash
            self.redis_conn.hmset("glossary:%s" % character_key, data)

            # Store the links in a list
            for i, link in enumerate(links):
                link_id = "%s:%i" % (character_key, i)
                self.redis_conn.rpush("glossary:%s:links" % character_key, link_id)
                self.redis_conn.hmset(
                    "glossary-link:%s" % link_id,
                    link,
                )


class MissionIndexer(object):
    """
    Takes a mission folder and indexes everything inside it.
    """

    def __init__(self, redis_conn, folder_path):
        self.redis_conn = redis_conn
        self.folder_path = folder_path
        self.mission_name = folder_path.strip("/").split("/")[-1]

    def index(self):
        # Delete the old things in the database
        # TODO: More sensible flush/switching behaviour
        self.redis_conn.flushdb()
        
        self.index_meta()
        self.index_transcripts()

    def index_transcripts(self):
        for filename in os.listdir(self.folder_path):
            if "." not in filename and filename[0] != "_" and filename[-1] != "~":
                print "Indexing %s..." % filename
                path = os.path.join(self.folder_path, filename)
                parser = TranscriptParser(path)
                indexer = TranscriptIndexer(self.redis_conn, self.mission_name, "%s/%s" % (self.mission_name, filename), parser)
                indexer.index()

    def index_meta(self):
        path = os.path.join(self.folder_path, "_meta")
        parser = MetaParser(path)
        indexer = MetaIndexer(self.redis_conn, self.mission_name, parser)
        indexer.index()


if __name__ == "__main__":
    redis_conn = redis.Redis()
    idx = MissionIndexer(redis_conn, os.path.join(os.path.dirname( __file__ ), '..', "transcripts/", "a13")) 
    idx.index()
    search_db.flush()
