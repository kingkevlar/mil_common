#!/usr/bin/env python

from __future__ import division
import rospy
import rosbag
import rostopic
import os
import resource
from collections import deque
import itertools
import datetime
from mil_ros_tools.srv import BaggerCommands

"""
To call call service call the '/online_bagger/dump' service with the BaggerCommands.srv
Amount and Unit. Doing this will generate a rosbag with the
last x number of seconds saved

For example:

        bagging_server = rospy.ServiceProxy('/online_bagger/dump', BaggerCommands)
        bag_status = bagging_server(bag_name, bag_time)
        bag_name = name of bag (leave blank to use default name: current date and time)
            Provide an empty string: '' to bag everything
        bag_time = float32 (leave blank to dump entire bag): 
            Provide 0.0, or 0 to bag everything
"""

class OnlineBagger(object):

    def __init__(self):

        """
        Make dictionary of dequeues.
        Subscribe to set of topics defined by the yaml file in directory
        Stream topics up to a given stream time, dump oldest messages when limit is reached
        Set up service to bag n seconds of data default to all of available data
        """

        self.successful_subscription_count = 0  # successful subscriptions
        self.iteration_count = 0  # number of iterations
        self.streaming = True
        self.get_params()
        self.make_dicts()

        self.bagging_service = rospy.Service('/online_bagger/dump', BaggerCommands,
                                             self.start_bagging)

        self.subscribe_loop()
        rospy.loginfo('subscriber list: %s', self.subscriber_list)

    def get_params(self):

        """
        Retrieve parameters from param server.
        """

        self.subscriber_list = rospy.get_param('/online_bagger/topics')
        self.dir = rospy.get_param('/online_bagger/bag_package_path', default=os.environ['HOME'])
        self.stream_time = rospy.get_param('/online_bagger/stream_time', default=30)  # seconds

        rospy.loginfo('subscriber list: %s', self.subscriber_list)
        rospy.loginfo('stream_time: %s seconds', self.stream_time)

    def make_dicts(self):

        """
        Make dictionary with sliceable deques() that will be filled with messages and time stamps.

        Subscriber list contains all of the topics, their stream time and their subscription status:
        A True status for a given topic corresponds to a successful subscription
        A False status indicates a failed subscription.

        Stream time for an individual topic is specified in seconds.

        For Example:
        self.subscriber_list[0:1] = [['/odom', 300 ,False], ['/absodom', 300, True]]

        Indicates that '/odom' has not been subscribed to, but '/absodom' has been subscribed to

        self.topic_messages is a dictionary of deques containing a list of tuples.
        Dictionary Keys contain topic names
        Each value for each topic contains a deque
        Each deque contains a list of tuples
        Each tuple contains a message and its associated time stamp

        For example:
        '/odom' is  a potential topic name
        self.topic_message['/odom']  is a deque
        self.topic_message['/odom'][0]  is the oldest message available in the deque
        and its time stamp if available. It is a tuple with each element: (time_stamp, msg)
        self.topic_message['/odom'][0][0] is the time stamp for the oldest message
        self.topic_message['/odom'][0][1] is the message associated with the oldest topic
        """

        self.topic_messages = {}
        self.topic_list = []

        class SliceableDeque(deque):
            def __getitem__(self, index):
                if isinstance(index, slice):
                    return type(self)(itertools.islice(self, index.start,
                                                       index.stop, index.step))
                return deque.__getitem__(self, index)

        for topic in self.subscriber_list:
            if len(topic) == 1:
                topic.append(self.stream_time)
                rospy.loginfo('no stream time provided, default used for: %s', topic)
            self.topic_messages[topic[0]] = SliceableDeque(deque())
            topic.append(False)
            self.topic_list.append(topic[0])

        rospy.loginfo('topics status: %s', self.subscriber_list)

    def subscribe_loop(self):

        """
        Continue to subscribe until at least one topic is successful,
        then break out of loop and be called in the callback function to prevent the function
        from locking up.
        """

        i = 0
        # if self.successful_subscription_count == 0 and not rospy.is_shutdown():
        while self.successful_subscription_count == 0 and not rospy.is_shutdown():
            self.subscribe()
            rospy.sleep(0.1)
            i = i + 1
            if i % 1000 == 0:
                rospy.logdebug('still subscribing!')

    def subscribe(self):

        """
        Subscribe to the topics defined in the yaml configuration file

        Function checks subscription status True/False of each topic
        if True: topic has already been sucessfully subscribed to
        if False: topic still needs to be subscribed to and
        subscriber will be run.

        Each element in self.subscriber list is a list [topic, Bool]
        where the Bool tells the current status of the subscriber (sucess/failure).

        Return number of topics that failed subscription
        """

        for topic in self.subscriber_list:
            if not topic[2]:
                msg_class = rostopic.get_topic_class(topic[0])
                if msg_class[1] is not None:
                    self.successful_subscription_count = self.successful_subscription_count + 1
                    rospy.Subscriber(topic[0], msg_class[0],
                                     lambda msg, _topic=topic[0]: self.bagger_callback(msg, _topic))

                    topic[2] = True  # successful subscription

    def get_topic_duration(self, topic):

        """
        Return current time duration of topic
        """

        return self.topic_messages[topic][-1][0] - self.topic_messages[topic][0][0]

    def get_header_time(self, msg):

        """
        Retrieve header time if available
        """

        if hasattr(msg, 'header'):
            return msg.header.stamp
        else:
            return rospy.get_rostime()

    def get_time_index(self, topic, requested_seconds):

        """
        Return the index for the time index for a topic at 'n' seconds from the end of the dequeue
        For example, to bag the last 10 seconds of data, the index for 10 seconds back from the most
        recent message can be obtained with this function.
        The number of requested seconds should be the number of seoncds desired from
        the end of deque. (ie. requested_seconds = 10 )
        If the desired time length of the bag is greater than the available messages it will output a
        message and return how ever many seconds of data are avaiable at the moment.
        Seconds is of a number type (not a rospy.Time type) (ie. int, float)
        """

        topic_duration = self.get_topic_duration(topic).to_sec()

        ratio = requested_seconds / topic_duration
        index = int(self.get_topic_message_count(topic) * (1 - min(ratio, 1)))

        self.bag_report = 'The requested %s seconds were bagged' % requested_seconds

        if index == 0:
            self.bag_report = 'Only %s seconds of the request %s seconds were available, all \
            messages were bagged' %  (topic_duration, requested_seconds)
        return index

    def bagger_callback(self, msg, topic):

        """
        Streaming callback function, stops streaming during bagging process
        also pops off msgs from dequeue if stream size is greater than specified stream_time

        Stream, callback function does nothing if streaming is not active
        """

        if not self.streaming:
            return

        self.iteration_count = self.iteration_count + 1
        time = self.get_header_time(msg)

        self.topic_messages[topic].append((time, msg))

        time_diff = self.get_topic_duration(topic)

        # verify streaming is popping off and recording topics
        if self.iteration_count % 100 == 0:
            rospy.logdebug('time_difference: %s', time_diff.to_sec())
            rospy.logdebug('topic: %s', topic)
            rospy.logdebug('topic type: %s', type(msg))
            rospy.logdebug('number of topic messages: %s', self.get_topic_message_count(topic))

        index = self.topic_list.index(topic)

        while time_diff > rospy.Duration(self.subscriber_list[index][1]) and not rospy.is_shutdown():
            self.topic_messages[topic].popleft()
            time_diff = self.get_topic_duration(topic)

        # re subscribe to failed topics if available later
        self.subscribe()

    def get_topic_message_count(self, topic):

        """
        Return number of messages available in a topic
        """

        return len(self.topic_messages[topic])

    def get_total_message_count(self):

        """
        Returns total number of messages across all topics
        """

        total_message_count = 0
        for topic in self.topic_messages.keys():
            total_message_count = total_message_count + self.get_topic_message_count(topic)

        return total_message_count

    def set_bag_directory(self,bag_name=''):

        """
        Create ros bag save directory
        If no bag name is provided, the current date/time is used as default.
        """

        types = ('', None, False)

        if bag_name in types:
            bag_name = str(datetime.date.today()) + '-' + str(datetime.datetime.now().time())[0:8]

        directory = os.path.join(self.dir, 'online_bagger/')

        rospy.loginfo('bag directory: %s', directory)
        rospy.loginfo('bag name: %s', bag_name)

        if not os.path.exists(directory):
            os.makedirs(directory)

        self.bag = rosbag.Bag(os.path.join(directory, bag_name + '.bag'), 'w')

    def set_bagger_status(self):

        """
        Set status of online bagger
        """

        self.bagger_status = 'Subscriber List: ' + str(self.subscriber_list) + ' Message Count: ' \
        + str(self.get_total_message_count())

    def start_bagging(self, req):

        """
        Dump all data in dictionary to bags, temporarily stops streaming
        during the bagging process, resumes streaming when over.
        """

        self.streaming = False
        self.set_bag_directory(req.bag_name)
        self.set_bagger_status()

        requested_seconds = req.bag_time

        for topic in self.subscriber_list:
            if topic[2] == False:
                continue

            topic = topic[0]
            rospy.loginfo('topic: %s', topic)

            # if no number is provided or a zero is given, bag all messages
            types = (0, 0.0, None)
            if req.bag_time in types:
                bag_index = 0
                self.bag_report = 'All messages were bagged'

            # get time index the normal way
            else:
                bag_index = self.get_time_index(topic, requested_seconds)

            messages = 0  # message number in a given topic
            for msgs in self.topic_messages[topic][bag_index:]:
                messages = messages + 1
                self.bag.write(topic, msgs[1], t=msgs[0])
                if messages % 100 == 0:  # print every 100th topic, type and time
                    rospy.loginfo('topic: %s, message type: %s, time: %s',
                    topic, type(msgs[1]), type(msgs[0]))

            # empty deque when done writing to bag
            self.topic_messages[topic].clear()

        rospy.loginfo('Bag Report: %s', self.bagger_status)
        self.bag.close()
        rospy.loginfo('bagging finished!')

        self.streaming = True
        return self.bagger_status

if __name__ == "__main__":
    rospy.init_node('online_bagger')
    stream = OnlineBagger()
    rospy.spin()
