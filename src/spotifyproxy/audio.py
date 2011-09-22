'''
Created on 12/05/2011

@author: mikel
'''
from spotify import link, BulkConditionChecker, session
from collections import deque



#General buffer error
class BufferError(IOError):
    pass



#Risen when stutter is detected
class BufferUnderrunError(BufferError):
    pass



class BufferInitializationError(BufferError):
    pass



class BufferStoppedError(BufferError):
    pass



class AbstractBuffer:
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
        pass

    
    def get_stats(self):
        pass
    
    
    def track_ended(self):
        pass



class QueueItem:
    data = None
    num_samples = None
    sample_type = None
    sample_rate = None
    num_channels = None
    frame_time = None
    
    def __init__(self, data, num_samples, sample_type, sample_rate, num_channels, frame_time):
        self.data = data
        self.num_samples = num_samples
        self.sample_type = sample_type
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.frame_time = frame_time



class TrackLoadCallback(session.SessionCallbacks):
    __checker = None
    
    
    def __init__(self, checker):
        self.__checker = checker
    
    
    def metadata_updated(self, session):
        self.__checker.check_conditions()



class AudioBuffer(AbstractBuffer):
    #Queue that holds the in-memory framen numbers
    __frames = None
    
    #Dict that holds the actual frame data
    __frame_data = None
    
    #Number of underruns since last get_stats() call
    __stutter = None
    
    #Flag indicating that the playback was stopped
    __playback_stopped = None
    
    #Configured buffer length in seconds
    __max_buffer_length = None
    
    #Current buffer length in seconds
    __buffer_length = None
    
    #Number of samples in buffer (not used but required by libspotify)
    __samples_in_buffer = None
    
    #Session instance
    __session = None
    
    #Last (and highest) requested frame by any client
    __last_frame = None
    
    #Frame flagged as the last one
    __end_frame = None
    
    #Currently playing track id
    __track_id = None
    
    #Currently playing track object
    __track = None
    
    
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
            raise BufferInitializationError("Failed loading track %s" % track_id)
        else:
            return track
    
    
    def __init__(self, session, track_id, max_buffer_length = 10):
        self.__frames = deque()
        self.__frame_data = {}
        self.__stutter = 0
        self.__playback_stopped = False
        self.__max_buffer_length = max_buffer_length
        self.__buffer_length = 0
        self.__samples_in_buffer = 0
        self.__session = session
        self.__last_frame = -1
        self.__end_frame = -1
        
        #Load the track
        self.__track_id = track_id
        self.__track = self._load_track(track_id)
        self.__session.player_load(self.__track)
    
    
    def start(self):
        self.__session.player_play(True)
    
    
    def _remove_first_frame(self):
        if len(self.__frames) > 0:
            frame_id = self.__frames[0]
            frame = self.__frame_data[frame_id]
            
            #Update sums
            self.__samples_in_buffer -= frame.num_samples
            self.__buffer_length -= frame.frame_time
            
            #Delete from the index first, then from the dict
            del self.__frames[0]
            del self.__frame_data[frame_id]
    
    
    def _append_frame(self, data, num_samples, sample_type, sample_rate, num_channels, frame_time):
        #Calculate the new frame id
        frame_id = self.get_last_frame_in_buffer() + 1
        
        #Save the data
        self.__frame_data[frame_id] = QueueItem(
            data,
            num_samples,
            sample_type,
            sample_rate,
            num_channels,
            frame_time,
        )
        
        #Update the buffer time
        self.__buffer_length += frame_time
        
        #Update the sample count
        self.__samples_in_buffer += num_samples
        
        #And finally index it on the queue
        self.__frames.append(frame_id)
        
        #Tell that all samples were consumed
        return num_samples
    
    
    def _will_fill_buffer(self, frame_time):
        return frame_time + self.__buffer_length > self.__max_buffer_length
    
    
    def _purge_frames(self, frame_time):
        while len(self.__frames) > 0:
            #Return if this frame cannot be deleted
            if self.__frames[0] == self.__last_frame:
                return False
            
            #It can be deleted, so let's do it
            elif self._will_fill_buffer(frame_time):
                self._remove_first_frame()
            
            #Previous tests passed. Frame will fit
            else:
                return True
    
    
    def get_first_frame_in_buffer(self):
        if len(self.__frames) > 0:
            return self.__frames[0]
        
        else:
            return -1
    
    
    def get_last_frame_in_buffer(self):
        if len(self.__frames) > 0:
            return self.__frames[-1]
        
        else:
            return -1
    
    
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
        #Calculate the length of this delivery in seconds
        frame_time = 1.0 * num_samples / sample_rate
        
        #Check if buffer is full, and purge if necessary
        if self._will_fill_buffer(frame_time) and not self._purge_frames(frame_time):
            #Tell that no frames where consumed
            return 0
        
        #Else append the data
        else:
            return self._append_frame(
                data, num_samples,
                sample_type, sample_rate,
                num_channels, frame_time
            )
    
    
    def get_stats(self):
        stutter = self.__stutter
        self.__stutter = 0
        return self.__samples_in_buffer, stutter
    
    
    def set_track_ended(self):
        self.__end_frame = self.get_last_frame_in_buffer()
    
    
    def get_frame(self, frame_num):
        #Raise error if buffer was stopped
        if self.__playback_stopped:
            raise BufferStoppedError()
        
        #What happens if this frame is not on the index?
        elif frame_num not in self.__frames:
            #If it's ahead of the buffer it's an underrun
            if frame_num > self.get_last_frame_in_buffer():
                self.__stutter += 1
                raise BufferUnderrunError("Frame #%d not yet available." % frame_num)
            
            #Otherwise this thread comes late (has been consumed by others)
            else:
                raise BufferError("Frame number #%d gone, too late my friend." % frame_num)
        
        #Let's serve the frame
        else:
            #Flag to indicate if there are frames left
            has_frames = frame_num != self.__end_frame
            
            #Store it (if higher) to prevent purge beyond this one
            if self.__last_frame < frame_num:
                self.__last_frame = frame_num
            
            #print "get frame #%d" % frame_num
            #print "frame_num(%d) != end_frame(%d): %d" % (frame_num, self.__end_frame, has_frames)
            
            return self.__frame_data[frame_num], has_frames
    
    
    def stop(self):
        if not self.__playback_stopped:
            self.__session.player_unload()
            self.__playback_stopped = True
    
    
    def is_stopped(self):
        return self.__playback_stopped
    
    
    def get_track_id(self):
        return self.__track_id
    
    
    def get_track(self):
        return self.__track



class BufferManager(AbstractBuffer):
    __current_buffer = None
    
    
    def _can_share_buffer(self, track_id):
        """
        Check if the requested track and the current one are the same.
        If true, check if the buffer is still on the start position, so
        this thread can catch up it.
        The result is a shared buffer between threads.
        """
        return(
            self.__current_buffer is not None and
            track_id == self.__current_buffer.get_track_id() and
            self.__current_buffer.get_first_frame_in_buffer() == 0
        )
    
    
    def open(self, session, track_id):
        #If we can't share this buffer start a new one
        if not self._can_share_buffer(track_id):
            #Stop current buffer if any
            if self.__current_buffer is not None:
                self.__current_buffer.stop()
            
            #Create the new buffer
            self.__current_buffer = AudioBuffer(session, track_id)
            
            #And start receiving data
            self.__current_buffer.start()
            
        return self.__current_buffer
    
    
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
        if self.__current_buffer is not None:
            return  self.__current_buffer.music_delivery(
                data, num_samples, sample_type, sample_rate, num_channels
            )
        else:
            return 0
    
    
    def get_stats(self):
        if self.__current_buffer is not None:
            return self.__current_buffer.get_stats()
    
    
    def set_track_ended(self):
        if self.__current_buffer is not None:
            self.__current_buffer.set_track_ended()
    
    
    def stop(self):
        if self.__current_buffer is not None:
            self.__current_buffer.stop()
