'''
Created on 06/05/2011

@author: mikel
'''

#Why the hell "import spotify" does not work?
from spotify import image as _image, BulkConditionChecker, link, session, SampleType
import threading, time, StringIO, cherrypy, re, struct
from audio import QueueItem, BufferStoppedError
from cherrypy import wsgiserver
from cherrypy.process import servers
import weakref
from datetime import datetime
import string, random
from utils import DynamicCallback

#TODO: urllib 3.x compatibility
import urllib2



class HTTPProxyError(Exception):
    pass



def format_http_date(dt):
    """
    As seen on SO, compatible with py2.4+:
    http://stackoverflow.com/questions/225086/rfc-1123-date-representation-in-python
    """
    """Return a string representation of a date according to RFC 1123
    (HTTP/1.1).

    The supplied date must be in UTC.

    """
    weekday = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]
    month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
             "Oct", "Nov", "Dec"][dt.month - 1]
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (weekday, dt.day, month,
        dt.year, dt.hour, dt.minute, dt.second)



def create_base_token(length=30):
    """
    Creates a random token with an optional length.
    Original from SO:
    http://stackoverflow.com/a/9011133/28581
    """
    pool = string.letters + string.digits
    return ''.join(random.choice(pool) for i in xrange(length))



def create_user_token(base_token, user_agent):
    return sha1sum(str.join('', [base_token, user_agent]))



def sha1sum(data):
    #SHA1 lib 2.4 compatibility
    try:
        from hashlib import sha1
        hash_obj = sha1()
    except:
        import sha
        hash_obj = sha.new()
    
    hash_obj.update(data)
    return hash_obj.hexdigest()



class ImageCallbacks(_image.ImageCallbacks):
    __checker = None
    
    
    def __init__(self, checker):
        self.__checker = checker
    
    
    def image_loaded(self, image):
        self.__checker.check_conditions()



class Image:
    __session = None
    __last_modified = None
    
    
    def __init__(self, session):
        self.__session = session
        self.__last_modified = format_http_date(datetime.utcnow())
    
    
    def _get_clean_image_id(self, image_str):
        #Strip the optional extension...
        r = re.compile('\.jpg$', re.IGNORECASE)
        return re.sub(r, '', image_str)
    
    
    @cherrypy.expose
    def default(self, image_id, **kwargs):
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
            cherrypy.response.headers["Last-Modified"] = self.__last_modified
            
            if method == 'GET':
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
    __base_token = None
    __allowed_ips = None
    
    
    def __init__(self, session, audio_buffer, base_token, allowed_ips, on_stream_ended):
        self.__session = session
        self.__audio_buffer = audio_buffer
        self.__base_token = base_token
        self.__allowed_ips = allowed_ips
        self.__is_playing = False
        self.__cb_stream_ended = on_stream_ended
    
    
    def _get_clean_track_id(self, track_str):
        #Fail if it's not a valid track id
        r  = re.compile('[a-z0-9]{22}(\.wav)?$', re.IGNORECASE)
        if r.match(track_str) is None:
            raise cherrypy.HTTPError(404)
        
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
    
    
    def _get_total_samples(self, frame, track):
        return frame.sample_rate * track.duration() / 1000
    
    
    def _generate_file_header(self, frame, num_samples):
        #Build the whole header
        return self._write_wave_header(
            num_samples, frame.num_channels, frame.sample_rate,
            self._get_sample_width(frame.sample_type)
        )
    
    
    def _write_file_content(self, buf, calc_samples, wave_header=None):
        #Write wave header
        if wave_header is not None:
            yield wave_header
        
        has_frames = True
        pad_with_silence = True
        frame_num = 0
        samples_written = 0
        
        #Loop while buffer tells to do so
        while has_frames:
            counter = 0
            file = StringIO.StringIO()
                
            #Write 10 frames at a time ~88k
            #TODO: Should check written size, instead of a fixed frame num?
            while counter < 10 and has_frames:
                try:
                    frame, has_frames = buf.get_frame_wait(frame_num)
                    
                    #Check if this frame fits in the estimated size
                    if samples_written + frame.num_samples < calc_samples:
                        file.write(frame.data)
                        samples_written += frame.num_samples
                    
                    #We need to truncate the frame data
                    else:
                        sample_width = self._get_sample_width(frame.sample_type)
                        sample_size = sample_width / 8 * frame.num_channels
                        samples_left = calc_samples - samples_written
                        truncate_size = samples_left * sample_size
                        file.write(frame.data[:truncate_size])
                        samples_written += samples_left
                        has_frames = False
                    
                    #Update counters
                    counter += 1
                    frame_num += 1
                
                #Handle gracefully a buffer cancellation
                except BufferStoppedError:
                    has_frames = False
                    pad_with_silence = False
            
            
            #This was the last write
            if not has_frames:
            
                #Check if we need to pad the file with silence
                if pad_with_silence and calc_samples > samples_written:
                    samples_left = calc_samples - samples_written
                    sample_width = self._get_sample_width(frame.sample_type)
                    sample_size = sample_width / 8 * frame.num_channels
                    padding_size = samples_left * sample_size
                    file.write('\0' * padding_size)
                    samples_written += samples_left
                    
                #Notify that the stream ended
                self.__cb_stream_ended()
            
            
            #Write the generated data
            yield file.getvalue()
    
    
    def _check_request(self):
        method = cherrypy.request.method.upper()
        headers = cherrypy.request.headers
        
        #Fail for other methods than get or head
        if method not in ("GET", "HEAD"):
            raise cherrypy.HTTPError(405)
        
        #Error if no token or user agent are provided
        if 'User-Agent' not in headers or 'X-Spotify-Token' not in headers:
            raise cherrypy.HTTPError(403)
        
        #Error if the requester is not allowed
        if headers['Remote-Addr'] not in self.__allowed_ips:
            raise cherrypy.HTTPError(403)
        
        #Check that the supplied token is correct
        user_token = headers['X-Spotify-Token']
        user_agent = headers['User-Agent']
        correct_token = create_user_token(self.__base_token, user_agent)
        if user_token != correct_token:
            raise cherrypy.HTTPError(403)
        
        return method
    
    
    def _write_http_headers(self, filesize):
        cherrypy.response.headers['Content-Type'] = 'audio/x-wav'
        cherrypy.response.headers['Content-Length'] = filesize
        cherrypy.response.headers['Accept-Ranges'] = 'bytes'
    
    
    def _parse_ranges(self):
        r = re.compile('^bytes=(\d*)-(\d*)$')
        m = r.match(cherrypy.request.headers['Range'])
        if m is not None:
            return m.group(1), m.group(2)
    
    
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
            raise RuntimeError("Failed loading track %s" % track_id)
        else:
            return track
    
    
    def _create_dummy_frame(self):
        """
        Create a dummy frame with default format.
        """
        return QueueItem(
            '',
            1,
            SampleType.Int16NativeEndian,
            44100,
            2,
            1.0 / 44100,
        )
    
    
    @cherrypy.expose
    def default(self, track_id, **kwargs):
        #Check sanity of the request
        self._check_request()
        
        #Get the requested clean track
        track_id = self._get_clean_track_id(track_id)
        
        #And load the track object
        track = self._load_track(track_id)
        
        #Get the first frame of the track
        if cherrypy.request.method.upper() == 'GET':
            buf = self.__audio_buffer.open(self.__session, track)
            frame, has_frames = buf.get_frame_wait(0)
        
        #Or just create a fake one
        else:
            frame = self._create_dummy_frame()
        
        
        #Calculate the total number of samples in the track
        num_samples = self._get_total_samples(frame, track)
        
        
        #It's a partial request
        if 'Range' in cherrypy.request.headers:
            #Calculate file size, and obtaine the header
            wave_header, filesize = self._generate_file_header(frame, num_samples)
            
            #Parse the ranges
            r1, r2 = self._parse_ranges()
            
            #TODO: should use serve_fileobj, instead of fiddling with ranges manually 
            
            #If we where asked to skip the header
            if r1 == str(len(wave_header)) and r2 == '':
                r1 = int(r1)
                args = (r1, filesize-1, filesize)
                cherrypy.response.status = 206
                cherrypy.response.headers['Content-Range'] = 'bytes %d-%d/%d' % args
                self._write_http_headers(filesize-r1)
                if cherrypy.request.method.upper() == 'GET':
                    return self._write_file_content(buf, num_samples)
            
            #For other situations throw an error
            else:
                cherrypy.response.status = 416
                self._write_http_headers(0)
        
        #A normal request
        else:
            #Calculate file size, and obtain the header
            wave_header, filesize = self._generate_file_header(frame, num_samples)
            self._write_http_headers(filesize)
            if cherrypy.request.method.upper() == 'GET':
                return self._write_file_content(buf, num_samples, wave_header)
            
    
    default._cp_config = {'response.stream': True}



class Root:
    __session = None
    
    image = None
    track = None
    
    
    def __init__(self, session, audio_buffer, base_token, allowed_ips, on_stream_ended):
        self.__session = session
        self.image = Image(session)
        self.track = Track(
            session, audio_buffer, base_token, allowed_ips, on_stream_ended
        )
    
    
    def cleanup(self):
        self.__session = None
        self.image = None
        self.track = None



class ProxyRunner(threading.Thread):
    __server = None
    __audio_buffer = None
    __base_token = None
    __allowed_ips = None
    __cb_stream_ended = None
    __root = None
    
    
    def _find_free_port(self, host, port_list):
        for port in port_list:
            try:
                servers.check_port(host, port, .1)
                return port
            except:
                pass
        
        list_str = ','.join([str(item) for item in port_list])
        raise HTTPProxyError("Cannot find a free port. Tried: %s" % list_str)
    
    
    def __init__(self, session, audio_buffer, host='localhost', try_ports=range(8080,8090), allowed_ips=['127.0.0.1']):
        port = self._find_free_port(host, try_ports)
        self.__audio_buffer = audio_buffer
        sess_ref = weakref.proxy(session)
        self.__base_token = create_base_token()
        self.__allowed_ips = allowed_ips
        self.__cb_stream_ended = DynamicCallback()
        self.__root = Root(
            sess_ref, audio_buffer, self.__base_token, self.__allowed_ips,
            self.__cb_stream_ended
        )
        app = cherrypy.tree.mount(self.__root, '/')
        self.__server = wsgiserver.CherryPyWSGIServer((host, port), app)
        threading.Thread.__init__(self)
    
    
    def set_stream_end_callback(self, callback):
        self.__cb_stream_ended.set_callback(callback)
    
    
    def clear_stream_end_callback(self):
        self.__cb_stream_ended.clear_callback();
        
    
    def run(self):
        self.__server.start()
    
    
    def get_port(self):
        return self.__server.bind_addr[1]
    
    
    def get_user_token(self, user_agent):
        return create_user_token(self.__base_token, user_agent)
    
    
    def ready_wait(self):
        while not self.__server.ready:
            time.sleep(.1)
    
    
    def stop(self):
        self.__audio_buffer.stop()
        self.__server.stop()
        self.join(10)
        self.__root.cleanup()
