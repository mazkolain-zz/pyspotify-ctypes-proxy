'''
Created on 12/05/2011

@author: mikel
'''
from collections import deque
import threading

from spotify.utils.decorators import synchronized

import copy



class AbstractBuffer:
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
        pass

    
    def get_stats(self):
        pass
    
    
    def track_ended(self):
        pass



class QueueItem:
    def __init__(self, **args):
            self.__dict__.update(args)



class AudioBuffer(AbstractBuffer):
    #Queue that holds the frames
    __queue = None
    
    #Number of underruns since last get_stats() call
    __stutter = None
    
    #Flag indicating that the track has reached it's end
    __track_ended = None
    
    #Flag indicating that the playback was canceled
    __playback_canceled = None
    
    #Total number of frame requests
    __frame_requests = None
    
    #Max buffer length in seconds
    max_buffer_length = 10
    
    #Session instance
    __session = None
    
    
    def __init__(self, session, track):
        self.__session = session
        self.__queue = deque()
        self.__stutter = 0
        self.__track_ended = False
        self.__playback_canceled = False
        self.__frame_requests = 0
        self.__session.player_load(track)
    
    
    def play(self):
        self.__session.player_play(True)
    
    
    def stop(self):
        pass
    
    
    def is_playing(self):
        return(
            not self.__playback_canceled
            and (len(self.__queue) > 0 or not self.__track_ended)
        )
    
    
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
        curtime = 1.0 * num_samples / sample_rate
        totaltime = self._get_buffer_time()
        
        #If buffer is full, return 0
        if totaltime + curtime > self.max_buffer_length:
            return 0
        
        #Otherwise append the data
        else:
            self.__queue.append(
                QueueItem(
                    data=data,
                    num_samples=num_samples,
                    sample_type=sample_type,
                    sample_rate=sample_rate,
                    num_channels=num_channels,
                )
            )
            
            return num_samples
    
    
    def _get_sample_count(self):
        queue = copy.copy(self.__queue)
        counter = 0
        for item in queue:
            counter += item.num_samples
        return counter
    
    
    def _get_buffer_time(self):
        queue = copy.copy(self.__queue)
        counter = 0
        for item in queue:
            counter += 1.0 * item.num_samples / item.sample_rate
        return counter
    
    
    def get_stats(self):
        #FIXME: Slow if called repeatedly, add some sort of caching
        stutter = self.__stutter
        self.__stutter = 0
        return self._get_sample_count(), stutter
    
    
    def set_track_ended(self):
        self.__track_ended = True
    
    
    def next_frame(self):
        self.__frame_requests += 1
        
        #While there are frames to send
        if self.is_playing():
            #Try to return the next frame
            try:
                return self.__queue.popleft()
            
            #Buffer was empty
            except IndexError:
                self.__stutter += 1
    
    
    def cancel(self):
        self.__playback_canceled = True
    
    
    def is_canceled(self):
        return self.__playback_canceled



class BufferManager(AbstractBuffer):
    __current_buffer = None
    
    
    def open(self, session, track):
        if self.__current_buffer is not None:
            self.__current_buffer.cancel()
        
        self.__current_buffer = AudioBuffer(session, track)
        
        return self.__current_buffer
    
    
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
       return  self.__current_buffer.music_delivery(
            data, num_samples, sample_type, sample_rate, num_channels
        )
    
    
    def get_stats(self):
        return self.__current_buffer.get_stats()
    
    
    def set_track_ended(self):
        self.__current_buffer.set_track_ended()

