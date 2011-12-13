'''
Created on 06/05/2011

@author: mikel
'''

#Why the hell "import spotify" does not work?
from spotify import image as _image, BulkConditionChecker, link, session, SampleType
import threading, time, StringIO, cherrypy, re, struct
from audio import BufferUnderrunError
from cherrypy import wsgiserver
import weakref



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
    
    
    def _get_clean_image_id(self, image_str):
        #Strip the optional extension...
        r = re.compile('\.jpg$', re.IGNORECASE)
        return re.sub(r, '', image_str)
    
    
    @cherrypy.expose
    def default(self, image_id):
        method = cherrypy.request.method.upper()
        
        #Fail for other methods than get or head
        if method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
        
        clean_image_id = self._get_clean_image_id(image_id)
        img = _image.create(self.__session, clean_image_id)
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
            cherrypy.response.headers["Content-Length"] = len(img.data())
            
            if method == 'GET':
                return img.data()



class Track:
    __session = None
    __audio_buffer = None
    __is_playing = None
    
    
    def __init__(self, session, audio_buffer):
        self.__session = session
        self.__audio_buffer = audio_buffer
        self.__is_playing = False
    
    
    def _get_clean_track_id(self, track_str):
        #Strip the optional extension...
        r = re.compile('\.wav$', re.IGNORECASE)
        return re.sub(r, '', track_str)
    
    
    def _write_wave_header(self, numsamples, channels, samplerate, bitspersample):
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
        
        return file.getvalue(), all_cunks_size + 8
    
    
    def _get_sample_width(self, sample_type):
        if sample_type == SampleType.Int16NativeEndian:
            return 16
        
        else:
            return -1
    
    
    def _generate_file_header(self, buf):
        has_frames = True
        
        while has_frames:
            try:
                frame, has_frames = buf.get_frame(0)
                track = buf.get_track()
                
                #Current sample duration (ms)
                framelen_ms = frame.frame_time * 1000
                
                #Calculate number of samples
                num_samples = track.duration() * frame.num_samples / framelen_ms
                
                #Build the whole header
                return self._write_wave_header(
                    num_samples, frame.num_channels, frame.sample_rate,
                    self._get_sample_width(frame.sample_type)
                )
            
            #Wait a bit if we are ahead of the buffer
            except BufferUnderrunError:
                time.sleep(0.1)
    
    
    def _write_frame_group(self, buf, start_frame_id):
        pass
    
    
    def _write_file_content(self, buf, wave_header):
        #Write wave header
        yield wave_header
        
        has_frames = True
        frame_num = 0
        
        #Loop while buffer tells to do so
        while has_frames:
            counter = 0
            file = StringIO.StringIO()
                
            #Write 10 frames at a time ~88k
            #TODO: Should check written size, instead of a fixed frame num?
            while counter < 10 and has_frames:
                try:
                    frame_data, has_frames = buf.get_frame(frame_num)
                    file.write(frame_data.data)
                    counter += 1
                    frame_num += 1
                    
                #We've gone ahead of the buffer, let's wait
                except BufferUnderrunError:
                    time.sleep(0.1)
            
            #Write the generated frame group
            yield file.getvalue()
    
    
    def _check_headers(self):
        method = cherrypy.request.method.upper()
        
        #Fail for other methods than get or head
        if method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
    
        #Ranges? not yet!
        elif "Range" in cherrypy.request.headers:
            raise cherrypy.HTTPError(416)
        
        return method
    
    
    def _write_http_headers(self, filesize):
        cherrypy.response.headers['Content-Type'] = 'audio/x-wav'
        cherrypy.response.headers['Content-Length'] = filesize
        cherrypy.response.headers['Accept-Ranges'] = 'none'
        cherrypy.response.headers['Connection'] = 'close'
    
    
    @cherrypy.expose
    def default(self, track_str):
        method = self._check_headers()
        
        #Ensure that the track object is loaded
        track_id = self._get_clean_track_id(track_str)
        #track = self._load_track(track_id)
        
        #Open the buffer
        buf = self.__audio_buffer.open(self.__session, track_id)
        
        #Calculate file size, and write headers (http and file)
        wave_header, filesize = self._generate_file_header(buf)
        self._write_http_headers(filesize)
        
        #Serve file contents if method was GET
        if method == 'GET':
            return self._write_file_content(buf, wave_header)
            
    
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
    __server = None
    __audio_buffer = None
    
    
    def __init__(self, session, audio_buffer):
        self.__audio_buffer = audio_buffer
        sess_ref = weakref.proxy(session)
        app = cherrypy.tree.mount(Root(sess_ref, audio_buffer), '/')
        self.__server = wsgiserver.CherryPyWSGIServer(('0.0.0.0', 8080), app)
        threading.Thread.__init__(self)
        
    
    def run(self):
        self.__server.start()
    
    
    def stop(self):
        self.__audio_buffer.stop()
        self.__server.stop()
