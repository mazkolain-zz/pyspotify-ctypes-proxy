'''
Created on 06/05/2011

@author: mikel
'''
import threading

#Why the hell "import spotify" does not work?
from spotify import image as _image, BulkConditionChecker, link, session, SampleType

import cherrypy



class ImageCallbacks(_image.ImageCallbacks):
    __checker = None
    
    
    def __init__(self, checker):
        self.__checker = checker
    
    
    def image_loaded(self, image):
        self.__checker.check_conditions()



class Image:
    __session = None
    
    
    def __init__(self, session):
        self.__session = session
    
    
    @cherrypy.expose
    def default(self, image_id):
        img = _image.create(self.__session, image_id)
        checker = BulkConditionChecker()
        checker.add_condition(img.is_loaded)
        img_cb = ImageCallbacks(checker)
        img.add_load_callback(img_cb)
        
        #Wait 30 secs or timeout
        checker.complete_wait(30)
        
        #Fail if image was not loaded or wrong format
        if not img.is_loaded() or img.format() != _image.ImageFormat.JPEG:
            raise cherrypy.HTTPError(500)
        
        else:
            cherrypy.response.headers["Content-Type"] = "image/jpeg"
            return img.data()



class TrackLoadCallback(session.SessionCallbacks):
    __checker = None
    
    
    def __init__(self, checker):
        self.__checker = checker
    
    
    def metadata_updated(self, session):
        self.__checker.check_conditions()



class Track:
    __session = None
    __audio_buffer = None
    __is_playing = None
    
    
    def __init__(self, session, audio_buffer):
        self.__session = session
        self.__audio_buffer = audio_buffer
        self.__is_playing = False
    
    
    def _load_track(self, track_id):
        full_id = "spotify:track:%s" % track_id
        track = link.create_from_string(full_id).as_track()
        
        #Set callbacks for loading the track
        checker = BulkConditionChecker()
        checker.add_condition(track.is_loaded)
        callbacks = TrackLoadCallback(checker)
        self.__session.add_callbacks(callbacks)
        
        #Wait until it's done (should be enough)
        checker.complete_wait(15)
        
        #Remove that callback, or will be around forever
        self.__session.remove_callbacks(callbacks)
        
        #Fail if after the wait it's still unusable
        if not track.is_loaded():
            raise cherrypy.HTTPError(500)
        else:
            return track
    
    
    def _write_wave_header(self, numsamples, channels, samplerate, bitspersample, initial_data):
        import StringIO, struct
        file = StringIO.StringIO()
        
        #Generate format chunk
        format_chunk_spec = "<4sLHHLLHH"
        format_chunk = struct.pack(
            format_chunk_spec,
            "fmt ", #Chunk id
            16, #Size of this chunk (excluding chunk id and this field)
            1, #Audio format, 1 for PCM
            channels, #Number of channels
            samplerate, #Samplerate, 44100, 48000, etc.
            samplerate * channels * (bitspersample / 8), #Byterate
            channels * (bitspersample / 8), #Blockalign
            bitspersample, #16 bits for two byte samples, etc.
        )
        
        #Generate data chunk
        data_chunk_spec = "<4sL"
        datasize = numsamples * channels * (bitspersample / 8)
        data_chunk = struct.pack(
            data_chunk_spec,
            "data", #Chunk id
            datasize, #Chunk size (excluding chunk id and this field)
        )
        
        sum_items = [
            #"WAVE" string following size field
            4,
            
            #"fmt " + chunk size field + chunk size
            struct.calcsize(format_chunk_spec),
            
            #Size of data chunk spec + data size
            struct.calcsize(data_chunk_spec) + datasize
        ]
        
        #Generate main header
        main_header_spec = "<4sL4s"
        main_header = struct.pack(
            main_header_spec,
            "RIFF",
            sum(sum_items),
            "WAVE"
        )
        
        #Write all the contents in
        file.write(main_header)
        file.write(format_chunk)
        file.write(data_chunk)
        file.write(initial_data)
        
        return file.getvalue()
    
    
    def _get_sample_width(self, sample_type):
        if sample_type == SampleType.Int16NativeEndian:
            return 16
        
        else:
            return -1
    
    
    def _write_file_header(self, track):
        import time
        
        while True:
            frame = self.__audio_buffer.next_frame()
            if frame is None:
                #Wait until there's at least one available
                time.sleep(0.1)
            
            else:
                #Current sample duration (ms)
                framelen_ms = frame.num_samples * 1.0 / (frame.sample_rate / 1000)
                
                #Calculate number of samples
                num_samples = track.duration() * frame.num_samples / framelen_ms
                
                #Build the whole header
                return self._write_wave_header(
                    num_samples, frame.num_channels, frame.sample_rate,
                    self._get_sample_width(frame.sample_type), frame.data
                )
    
    
    def _write_frames(self):
        import StringIO, time
        
        counter = 0
        file = StringIO.StringIO()
        while counter < 10:
            frame = self.__audio_buffer.next_frame()
            if frame is not None:
                file.write(frame.data)
                counter += 1
            else:
                #A little bit of punishment
                time.sleep(0.1)
        
        return file.getvalue()
    
    
    @cherrypy.expose
    def default(self, track_id):
        cherrypy.response.headers['Content-Type'] = 'audio/x-wav'
        
        #Ensure that the track object is loaded
        track = self._load_track(track_id)
        
        #Load track audio...
        #these should go to somewhere like _populate_buffer
        self.__session.player_load(track)
        self.__session.player_play(True)
        
        #Write the file header
        yield self._write_file_header(track)
        
        #Write the actual content
        while True:
            yield self._write_frames()
        
    
    default._cp_config = {'response.stream': True}



class Root:
    __session = None
    
    image = None
    track = None
    
    
    def __init__(self, session, audio_buffer):
        self.__session = session
        self.image = Image(session)
        self.track = Track(session, audio_buffer)



class ProxyRunner(threading.Thread):
    def __init__(self, session, audio_buffer):
        threading.Thread.__init__(self)
        cherrypy.tree.mount(Root(session, audio_buffer), "/")
    
    def run(self):
        cherrypy.engine.start()
        cherrypy.engine.block()
    
    def stop(self):
        cherrypy.engine.exit()
