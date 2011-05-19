'''
Created on 12/05/2011

@author: mikel
'''
from collections import deque
import threading

from spotify.utils.decorators import synchronized

import copy


class QueueItem:
    def __init__(self, **args):
            self.__dict__.update(args)



class MemoryBuffer:
    __queue = None
    __stutter = None
    __track_ended = None
    
    __frame_requests = 0
    
    #Max buffer length in seconds
    max_buffer_length = 10
    
    
    def __init__(self):
        self.__queue = deque()
        self.clear()
    
    
    def music_delivery(self, data, num_samples, sample_type, sample_rate, num_channels):
        #print "music delivery on MemoryBuffer"
        curtime = 1.0 * num_samples / sample_rate
        totaltime = self._get_buffer_time()
        
        #print "buffer length: %d" % totaltime
        
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
            #print "md: queue len: %d" % len(self.__queue)
            return num_samples
    
    
    def clear(self):
        self.__queue.clear()
        self.__stutter = 0
    
    
    def _next_frame(self):
        return self.__queue.popleft()
    
    
    def next_frame(self):
        self.__frame_requests += 1
        
        #Try to return the next frame
        try:
            return self.__queue.popleft()
        
        #Buffer was empty
        except IndexError:
            self.__stutter += 1
    
    
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
