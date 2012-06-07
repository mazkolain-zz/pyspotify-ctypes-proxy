'''
Created on 12/05/2011

@author: mikel
'''
import time
from spotify import link, BulkConditionChecker, session
from collections import deque
import threading



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



class AudioBuffer(AbstractBuffer):
    #OMG! It's full of vars!
    
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
    
    #Total served time
    __served_time = None
    
    #Number of samples in buffer (not used but required by libspotify)
    __samples_in_buffer = None
    
    #Total samples delivered by libspotify
    __total_samples = None
    
    #Estimated number of total samples in track
    __calc_total_samples = None
    
    #Session instance
    __session = None
    
    #Last (and highest) requested frame by any client
    __last_frame = None
    
    #Frame flagged as the last one
    __end_frame = None
    
    #Currently playing track object
    __track = None
    
    #Buffer pos update locl
    __served_time_lock = None
    
    
    
    def __init__(self, session, track, max_buffer_length = 10):
        self.__frames = deque()
        self.__frame_data = {}
        self.__stutter = 0
        self.__playback_stopped = False
        self.__max_buffer_length = max_buffer_length
        self.__buffer_length = 0
        self.__samples_in_buffer = 0
        self.__total_samples = 0
        self.__session = session
        self.__last_frame = -1
        self.__end_frame = -1
        self.__served_time = 0
        self.__served_time_lock = threading.Lock()
        
        #Load the track
        self.__track = track
        self.__session.player_load(self.__track)
    
    
    def start(self):
        #Start receiving data
        self.__session.player_play(True)
        
        #Now that we may have data, calculate number of samples
        frame, has_frames = self.get_frame_wait(0)
        track = self.get_track()
        framelen_ms = frame.frame_time * 1000
        self.__calc_total_samples = int(
            track.duration() * frame.num_samples / framelen_ms
        )
    
    
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
        
        #Update the buffer
        self.__buffer_length += frame_time
        
        
        #Update the sample counts
        self.__samples_in_buffer += num_samples
        self.__total_samples += num_samples
        
        #And finally index it on the queue
        self.__frames.append(frame_id)
        
        #Tell that all samples were consumed
        return num_samples
    
    
    def _will_fill_buffer(self, frame_time):
        return frame_time + self.__buffer_length > self.__max_buffer_length
    
    
    def _purge_frames(self):
        while len(self.__frames) > 0:
            #Never purge frames if we served less than 10s from the start
            if self.__served_time < 10:
                break
            
            #Break if reached to an undeletable frame
            if self.__frames[0] == self.__last_frame:
                break
            
            #Delete the first one
            else:
                self._remove_first_frame()
    
    
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
        
        #If buffer is full, purge and return
        if self._will_fill_buffer(frame_time):
            self._purge_frames()
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
    
    
    def get_total_samples(self):
        return self.__total_samples
    
    
    def get_calc_total_samples(self):
        return self.__calc_total_samples
    
    
    def set_track_ended(self):
        self.__end_frame = self.get_last_frame_in_buffer()
    
    
    def _update_served_time(self, frame):
        self.__served_time_lock.acquire()
        try:
            self.__served_time += frame.frame_time
        finally:
            self.__served_time_lock.release()
    
    
    def get_frame(self, frame_num):
        #Raise error if buffer was stopped
        if self.__playback_stopped:
            raise BufferStoppedError()
        
        #What happens if this frame is not on the index?
        elif frame_num not in self.__frames:
            #Frame is no longer available
            if frame_num < self.get_first_frame_in_buffer():
                raise BufferError("Frame number #%d gone, too late my friend." % frame_num)
            
            #If it's ahead of the buffer, it's an underrun
            else:
                self.__stutter += 1
                raise BufferUnderrunError("Frame #%d not yet available." % frame_num)
        
        #Let's serve the frame
        else:
            #Get requested frame
            frame = self.__frame_data[frame_num]
            
            #Update some counters
            self._update_served_time(frame)
            
            #Store it (if higher) to prevent purge beyond this one
            if self.__last_frame < frame_num:
                self.__last_frame = frame_num
            
            #Flag to indicate if there are frames left
            has_frames = frame_num != self.__end_frame
            
            return frame, has_frames
    
    
    def get_frame_wait(self, frame_num):
        has_frames = True
        
        while has_frames:
            try:
                return self.get_frame(frame_num)
            
            #Wait a bit if we are ahead of the buffer
            except BufferUnderrunError:
                time.sleep(0.1)
    
    
    def stop(self):
        if not self.__playback_stopped:
            self.__session.player_unload()
            self.__playback_stopped = True
    
    
    def is_stopped(self):
        return self.__playback_stopped
    
    
    def get_track(self):
        return self.__track



class BufferManager(AbstractBuffer):
    __current_buffer = None
    __buffer_size = None
    
    
    def __init__(self, buffer_size = 10):
        self.__buffer_size = buffer_size
    
    
    def _can_share_buffer(self, track):
        """
        Check if the requested track and the current one are the same.
        If true, check if the buffer is still on the start position, so
        this thread can catch up it.
        The result is a shared buffer between threads.
        """
        return(
            self.__current_buffer is not None and
            str(track) == str(self.__current_buffer.get_track()) and
            self.__current_buffer.get_first_frame_in_buffer() == 0
        )
    
    
    def open(self, session, track):
        #If we can't share this buffer start a new one
        if not self._can_share_buffer(track):
            #Stop current buffer if any
            if self.__current_buffer is not None:
                self.__current_buffer.stop()
            
            #Create the new buffer
            self.__current_buffer = AudioBuffer(
                session, track, self.__buffer_size
            )
            
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
