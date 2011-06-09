'''
Created on 06/05/2011

@author: mikel
'''
import threading

#Why the hell "import spotify" does not work?
from spotify import image as _image, BulkConditionChecker, link, session, SampleType

import cherrypy

import re


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
        #Strip the optional extension...
        r = re.compile('\.wav$', re.IGNORECASE)
        track_id = re.sub(r, '', track_id)
        
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
            int(datasize), #Chunk size (excluding chunk id and this field)
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
        all_cunks_size = int(sum(sum_items))
        main_header_spec = "<4sL4s"
        main_header = struct.pack(
            main_header_spec,
            "RIFF",
            all_cunks_size,
            "WAVE"
        )
        
        #Write all the contents in
        file.write(main_header)
        file.write(format_chunk)
        file.write(data_chunk)
        file.write(initial_data)
        
        return file.getvalue(), all_cunks_size + 8
    
    
    def _get_sample_width(self, sample_type):
        if sample_type == SampleType.Int16NativeEndian:
            return 16
        
        else:
            return -1
    
    
    def _write_file_header(self, buf, track):
        import time
        
        while buf.is_playing():
            frame = buf.next_frame()
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
    
    
    def _write_frames(self, buf):
        import StringIO, time
        
        counter = 0
        file = StringIO.StringIO()
        
        while counter < 10 and buf.is_playing():
            frame = buf.next_frame()
            if frame is not None:
                file.write(frame.data)
                counter += 1
            else:
                #A little bit of punishment
                time.sleep(0.1)
        
        return file.getvalue()
    
    
    def _stream_output(self, buf, initial_data):
        yield initial_data
        
        buf.play()
        
        #Write the actual content
        while buf.is_playing():
            yield self._write_frames(buf)
        
        if buf.is_canceled():
            raise cherrypy.HTTPError(500)
        
        #Inform libspotify
        #self.__session.player_unload()
    
    
    def _check_headers(self):
        method = cherrypy.request.method.upper()
        
        #Fail for other methods than get or head
        if method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
    
        #Ranges? not yet!
        elif "Range" in cherrypy.request.headers:
            raise cherrypy.HTTPError(416)
        
        return method
    
    
    def _write_headers(self, filesize):
        cherrypy.response.headers['Content-Type'] = 'audio/x-wav'
        cherrypy.response.headers['Content-Length'] = filesize
        cherrypy.response.headers['Accept-Ranges'] = 'none'
        cherrypy.response.headers['Connection'] = 'close'
    
    
    @cherrypy.expose
    def default(self, track_id):
        method = self._check_headers()
        
        #Ensure that the track object is loaded
        track = self._load_track(track_id)
        
        #Open the buffer
        buf = self.__audio_buffer.open(self.__session, track)
        
        #Load track audio...
        #these should go to somewhere like _populate_buffer
        #self.__audio_buffer.clear()
        
        #Calculate file size, and tell it
        initial_data, filesize = self._write_file_header(buf, track)
        self._write_headers(filesize)
        
        #If method was get, stream the actual content
        if method == 'GET':
            return self._stream_output(buf, initial_data)
    
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
        cherrypy.config.update({
            'engine.autoreload_on': False,
        })
        cherrypy.tree.mount(Root(session, audio_buffer), "/")
        
    
    def run(self):
        cherrypy.engine.start()
        cherrypy.engine.block()
    
    def stop(self):
        cherrypy.engine.exit()
