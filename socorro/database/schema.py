import psycopg2 as pg
import datetime as dt
import threading

import socorro.lib.prioritize as socorro_pri
import socorro.lib.psycopghelper as socorro_psy
import socorro.database.postgresql as socorro_pg

import socorro.lib.util as socorro_util
"""
Schema.py contains several utility functions and the code which describes all the database tables used by socorro. 
"""

#-----------------------------------------------------------------------------------------------------------------
def mondayPairsIteratorFactory(minDate, maxDate):
  """
  Given a pair of dates, creates iterator that returns (aMonday,theNextMonday) such that
    - the first returned pair defines an interval holding minDate
    - the last returned pair defines an interval holding maxDate
  if minDate or maxDate are not instances of datetime.date, raises TypeError
  if maxDate > minDate, raises ValueError
  """
  if not (isinstance(minDate,dt.date) and isinstance(maxDate,dt.date)):
    raise TypeError("minDate and maxDate must be instances of datetime.date")
  if maxDate < minDate:
    raise ValueError("minDate must be <= maxDate")
  def anIterator():
    oneWeek = dt.timedelta(7)
    aDate = minDate - dt.timedelta(minDate.weekday()) # begin on Monday before minDate
    while aDate <= maxDate:
      nextMonday = aDate + oneWeek
      yield (aDate, nextMonday)
      aDate = nextMonday
  return anIterator()

#-----------------------------------------------------------------------------------------------------------------
# For each database TableClass below,
# databaseDependenciesForSetup[TableClass] = [List of TableClasses on which this TableClass depends]
# NOTE: This requires that new Tables be added textually below every Table on which they depend
databaseDependenciesForSetup = {}
def getOrderedSetupList(whichTables = None):
  """
  A helper function to get the correct order to create tables during setup.
  whichTables is a list of Tables, possibly empty, or None
  If not whichTables, then all the known tables are visited
  """
  # if whichTables is None, then databaseDependenciesForSetup.keys() is used
  return socorro_pri.dependencyOrder(databaseDependenciesForSetup,whichTables)
databaseDependenciesForPartition = {}
def getOrderedPartitionList(whichTables):
  """
  A helper function to get the needed PartionedTables for a given set of PartitionedTables
  """
  if not whichTables:
    return []
  order = socorro_pri.dependencyOrder(databaseDependenciesForPartition,whichTables)
  return order

# This set caches knowledge of existing partition tables to avoid hitting database. Beware cache incoherence
partitionCreationHistory = set()
#-----------------------------------------------------------------------------------------------------------------
def partitionWasCreated(partitionTableName):
  """Helper function to examine partitionCreationHistory"""
  return partitionTableName in partitionCreationHistory
#-----------------------------------------------------------------------------------------------------------------
def markPartitionCreated(partitionTableName):
  """Helper function to update partitionCreationHistory"""
  global partitionCreationHistory
  partitionCreationHistory.add(partitionTableName)

#=================================================================================================================
class PartitionControlParameterRequired(Exception):
  def __init__(self):
    super(PartitionControlParameterRequired, self).__init__("No partition control paramter was supplied")

#=================================================================================================================
class DatabaseObject(object):
  """
  Base class for all objects (Tables, Constraints, Indexes) that may be individually created and used in the database
  Classes that inherit DatabaseObject:
   - Must supply appropriate creationSql parameter to the superclass constructor
   - May override method additionalCreationProcedure(self,aDatabaseCursor). If this is provided, it is
     called after creationSql is executed in method create(self,aDatabaseCursor)
     The cursor's connection is neither committed nor rolled back during the call to create
   - May override methods which do nothing in this class:
       = drop(self,aDatabaseCursor)
       = updateDefinition(self,aDatabaseCursor)
       = createPartitions(self,aDatabaseCursor,aPartitionDetailsIterator)
   Every leaf class that inherits DatabaseObject should be aware of the module-level dictionary: databaseDependenciesForSetup.
   If that leaf class should be created when the database is being set up, the class itself must be added as a key in the
   databaseDependenciesForSetup dictionary. The value associated with that key is a possibly empty iterable containing the
   classes on which the particular leaf class depends: Those that must already be created before the particular instance is
   created. This is often because the particular table has one or more foreign keys referencing tables upon which it depends.
   """
  #-----------------------------------------------------------------------------------------------------------------
  def __init__(self, name=None, logger=None, creationSql=None, **kwargs):
    super(DatabaseObject, self).__init__()
    self.name = name
    self.creationSql = creationSql
    self.logger = logger
  #-----------------------------------------------------------------------------------------------------------------
  def _createSelf(self,databaseCursor):
    databaseCursor.execute(self.creationSql)
    self.additionalCreationProcedures(databaseCursor)
  #-----------------------------------------------------------------------------------------------------------------
  def create(self, databaseCursor):
    orderedDbObjectList = getOrderedSetupList([self.__class__])
    for dbObjectClass in orderedDbObjectList:
      dbObjectObject = self
      if not self.__class__ == dbObjectClass:
        dbObjectObject = dbObjectClass(logger = self.logger)
      databaseCursor.execute("savepoint creating_%s"%dbObjectObject.name)
      try:
        dbObjectObject._createSelf(databaseCursor)
        databaseCursor.execute("release savepoint creating_%s"%dbObjectObject.name)
      except pg.ProgrammingError,x:
        databaseCursor.execute("rollback to creating_%s"%dbObjectObject.name)
        databaseCursor.connection.commit()
        self.logger.debug("%s - in create for %s, dbObject %s exists",threading.currentThread().getName(),self.name,dbObjectObject.name)

  #-----------------------------------------------------------------------------------------------------------------
  def additionalCreationProcedures(self, databaseCursor):
    pass
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    pass
  #-----------------------------------------------------------------------------------------------------------------
  def drop(self, databaseCursor):
    pass
  #-----------------------------------------------------------------------------------------------------------------
  def createPartitions(self, databaseCursor, iterator):
    pass

#=================================================================================================================
class Table (DatabaseObject):
  """
  Base class for all Table objects that may be created and used in the database.
  Classes that inherit DatabaseObject:
   - Must supply appropriate creationSql parameter to the superclass constructor
   - May override method insert(self,rowTuple, **kwargs) to do the right thing during an insert
   - May provide method alterColumnDefinitions(self,aDatabaseCursor,tableName)
   - May provide method updateDefinition(self,aDatabaseCursor)
   - Must be aware of databaseDependenciesForSetup and how it is used
  class Table inherits method create from DatabaseObject
  class Table provides a reasonable implementation of method drop, overriding the empty one in DatabaseObject
  """
  #-----------------------------------------------------------------------------------------------------------------
  def __init__(self, name=None, logger=None, creationSql=None, **kwargs):
    super(Table, self).__init__(name=name, logger=logger, creationSql=creationSql, **kwargs)
  #-----------------------------------------------------------------------------------------------------------------
  def drop(self, databaseCursor):
    databaseCursor.execute("drop table if exists %s cascade" % self.name)
  #-----------------------------------------------------------------------------------------------------------------
  def insert(self, rowTuple=None, **kwargs):
    pass

#=================================================================================================================
class PartitionedTable(Table):
  """
  Base class for Tables that will be partitioned or are likely to be programmatically altered.
  Classes that inherit PartitionedTable
   - Must supply self.insertSql with 'TABLENAME' replacing the actual table name
   - Must supply appropriate creationSql and partitionCreationSqlTemplate to the superclass constructor
   - Should NOT override method insert, which does something special for PartitionedTables
   - May override method partitionCreationParameters(self, partitionDetails) which returns a dictionary suitable for string formatting

   Every leaf class that inherits PartitionedTable should be aware of the module-level dictionary: databaseDependenciesForPartition
   If that leaf class has a partition that depends upon some other partition, then it must be added as a key to the dictionary
   databaseDependenciesForPartition. The value associated with that key is an iterable containing the classes that define the partitions
   on which this particular leaf class depends: Those that must already be created before the particular instance is created. This is
   most often because the particular partition table has one or more foreign keys referencing partition tables upon which it depends.
  """
  #-----------------------------------------------------------------------------------------------------------------
  partitionCreationLock = threading.RLock()
  def __init__ (self, name=None, logger=None, creationSql=None, partitionNameTemplate='%s', partitionCreationSqlTemplate='', weekInterval=None, **kwargs):
    super(PartitionedTable, self).__init__(name=name, logger=logger, creationSql=creationSql)
    self.partitionNameTemplate = partitionNameTemplate
    self.partitionCreationSqlTemplate = partitionCreationSqlTemplate
    self.weekInterval = weekInterval
    if not weekInterval:
      today = dt.date.today()
      self.weekInterval = mondayPairsIteratorFactory(today,today)
    self.insertSql = None

  #-----------------------------------------------------------------------------------------------------------------
  #def additionalCreationProcedures(self, databaseCursor):
    #self.createPartitions(databaseCursor, self.weekInterval)
  #-----------------------------------------------------------------------------------------------------------------
  def _createOwnPartition(self, databaseCursor, uniqueItems):
    """
    Internal method that assumes all precursor partitions are already in place before creating this one. Called
    from createPartitions(same parameters) to avoid bottomless recursion. Creates one or more partitions for
    this particular table, (more if uniqueItems has more than one element)
    side effect: Cursor's connection has been committed() by the time we return
    """
    self.logger.debug("%s - in createOwnPartition for %s",threading.currentThread().getName(),self.name)
    for x in uniqueItems:
      #self.logger.debug("DEBUG - item value is %s",x)
      partitionCreationParameters = self.partitionCreationParameters(x)
      partitionName = self.partitionNameTemplate % partitionCreationParameters["partitionName"]
      if partitionWasCreated(partitionName):
        #self.logger.debug("DEBUG - skipping creation of %s",partitionName)
        continue
      partitionCreationSql = self.partitionCreationSqlTemplate % partitionCreationParameters
      #self.logger.debug("%s - Sql for %s is %s",threading.currentThread().getName(),self.name,partitionCreationSql)
      aPartition = Table(name=partitionName, logger=self.logger, creationSql=partitionCreationSql)
      self.logger.debug("%s - savepoint createPartitions_%s",threading.currentThread().getName(), partitionName)
      databaseCursor.execute("savepoint createPartitions_%s" % partitionName)
      try:
        self.logger.debug("%s - creating %s", threading.currentThread().getName(), partitionName)
        aPartition._createSelf(databaseCursor)
        markPartitionCreated(partitionName)
        self.logger.debug("%s - successful - releasing savepoint", threading.currentThread().getName())
        databaseCursor.execute("release savepoint createPartitions_%s" % partitionName)
      except pg.ProgrammingError, x:
        self.logger.debug("%s -- Rolling back and releasing savepoint: Creating %s failed in createPartitions: %s", threading.currentThread().getName(), partitionName, str(x).strip())
        databaseCursor.execute("rollback to createPartitions_%s; release savepoint createPartitions_%s;" % (partitionName, partitionName))
      databaseCursor.connection.commit()

  #-----------------------------------------------------------------------------------------------------------------
  def createPartitions(self, databaseCursor, iterator):
    """
    Create this table's partition(s) and all the precursor partition(s) needed to support this one
    databaseCursor: as always
    iterator: Supplies at least one unique identifier (a date). If more than one then more than one (family of)
              partition(s) is created
    side effects: The cursor's connection will be rolled back or committed by the end of this method
    """
    self.logger.debug("%s - in createPartitions", threading.currentThread().getName())
    partitionTableClasses = getOrderedPartitionList([self.__class__])
    #self.logger.debug("DEBUG - Classes are %s",partitionTableClasses)
    uniqueItems = [x for x in iterator]
    for tableClass in partitionTableClasses:
      tableObject = self
      if not self.__class__ == tableClass:
        tableObject = tableClass(logger = self.logger)
      #self.logger.debug("DEBUG - Handling %s /w/ sql %s",tableObject.name,tableObject.partitionCreationSqlTemplate)
      tableObject._createOwnPartition(databaseCursor,uniqueItems)

  #-----------------------------------------------------------------------------------------------------------------
  def partitionCreationParameters(self,partitioningData):
    """returns: a dictionary of string substitution parameters"""
    return {}
  #-----------------------------------------------------------------------------------------------------------------
  def updateColumnDefinitions(self, databaseCursor):
    childTableList = socorro_pg.childTablesForTable(self.name, databaseCursor)
    for aChildTableName in childTableList:
      databaseCursor.execute("alter table %s no inherit %s", (aTable, aChildTableName))
    self.alterColumnDefinitions(databaseCursor, self.name)
    for aChildTableName in childTableList:
      self.alterColumnDefinitions(databaseCursor, aChildTableName)
    for aChildTableName in childTableList:
      databaseCursor.execute("alter table %s inherit %s", (aTable, aChildTableName))
  #-----------------------------------------------------------------------------------------------------------------
  def insert(self, databaseCursor, row, alternateCursorFunction, **kwargs):
    try:
      uniqueIdentifier = kwargs["date_processed"]
    except KeyError:
      raise PartitionControlParameterRequired()
    dateRangeTuple = mondayPairsIteratorFactory(uniqueIdentifier, uniqueIdentifier).next()# create iterator and throw away
    partitionName = self.partitionCreationParameters(dateRangeTuple)["partitionName"]
    insertSql = self.insertSql.replace('TABLENAME', partitionName)
    try:
      databaseCursor.execute("savepoint %s" % partitionName)
      #self.logger.debug("%s - Trying to insert into %s", threading.currentThread().getName(), self.name)
      databaseCursor.execute(insertSql, row)
      databaseCursor.execute("release savepoint %s" % partitionName)
    except pg.ProgrammingError, x:
      self.logger.debug('%s - Rolling back and releasing savepoint: failed: %s', threading.currentThread().getName(), str(x).strip())
      databaseCursor.execute("rollback to %s; release savepoint %s;" % (partitionName, partitionName))
      databaseCursor.connection.commit() # This line added after of hours of blood, sweat, tears. Remove only per deathwish.

      altConnection, altCursor = alternateCursorFunction()
      dateIterator = mondayPairsIteratorFactory(uniqueIdentifier, uniqueIdentifier)
      try:
        self.createPartitions(altCursor,dateIterator)
      except pg.DatabaseError,x:
        self.logger.debug("%s - Failed to create partition(s) %s: %s:%s", threading.currentThread().getName(), partitionName, type(x), x)
      self.logger.debug("%s - trying to insert into %s for the second time", threading.currentThread().getName(), self.name)
      databaseCursor.execute(insertSql, row)

#=================================================================================================================
class ReportsTable(PartitionedTable):
  """Define the table 'reports'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(ReportsTable, self).__init__(name='reports', logger=logger,
                                       creationSql="""
                                          CREATE TABLE reports (
                                              id serial NOT NULL,
                                              client_crash_date timestamp with time zone,
                                              date_processed timestamp without time zone,
                                              uuid character varying(50) NOT NULL,
                                              product character varying(30),
                                              version character varying(16),
                                              build character varying(30),
                                              signature character varying(255),
                                              url character varying(255),
                                              install_age integer,
                                              last_crash integer,
                                              uptime integer,
                                              cpu_name character varying(100),
                                              cpu_info character varying(100),
                                              reason character varying(255),
                                              address character varying(20),
                                              os_name character varying(100),
                                              os_version character varying(100),
                                              email character varying(100),
                                              build_date timestamp without time zone,
                                              user_id character varying(50),
                                              started_datetime timestamp without time zone,
                                              completed_datetime timestamp without time zone,
                                              success boolean,
                                              truncated boolean,
                                              processor_notes text,
                                              user_comments character varying(1024),
                                              app_notes character varying(1024),
                                              distributor character varying(20),
                                              distributor_version character varying(20),
                                              topmost_filenames TEXT,
                                              addons_checked boolean,
                                              flash_version TEXT,
                                              hangid TEXT,
                                              process_type TEXT
                                          );
                                          --CREATE TRIGGER reports_insert_trigger
                                          --    BEFORE INSERT ON reports
                                          --    FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""",
                                       partitionCreationSqlTemplate="""
                                          CREATE TABLE %(partitionName)s (
                                              CONSTRAINT %(partitionName)s_date_check CHECK (TIMESTAMP without time zone '%(startDate)s' <= date_processed and date_processed < TIMESTAMP without time zone '%(endDate)s'),
                                              CONSTRAINT %(partitionName)s_unique_uuid unique (uuid),
                                              PRIMARY KEY(id)
                                          )
                                          INHERITS (reports);
                                          CREATE INDEX %(partitionName)s_date_processed_key ON %(partitionName)s (date_processed);
                                          CREATE INDEX %(partitionName)s_uuid_key ON %(partitionName)s (uuid);
                                          CREATE INDEX %(partitionName)s_url_key ON %(partitionName)s (url);
                                          CREATE INDEX %(partitionName)s_build_key ON %(partitionName)s (build);
                                          CREATE INDEX %(partitionName)s_product_version_key ON %(partitionName)s (product, version);
                                          CREATE INDEX %(partitionName)s_signature_date_processed_build_key ON %(partitionName)s (signature, date_processed, build);
                                          CREATE INDEX %(partitionName)s_hangid_idx ON %(partitionName)s (hangid);
                                          """
                                      )
    self.columns = ("uuid", "client_crash_date", "date_processed", "product", "version", "build", "url", "install_age", "last_crash", "uptime", "email", "build_date", "user_id", "user_comments", "app_notes", "distributor", "distributor_version", "topmost_filenames", "addons_checked", "flash_version", "hangid", "process_type")
    self.insertSql = """insert into TABLENAME
                            (uuid, client_crash_date, date_processed, product, version, build, url, install_age, last_crash, uptime, email, build_date, user_id, user_comments, app_notes, distributor, distributor_version, topmost_filenames, addons_checked, flash_version, hangid, process_type) values
                            (%s,   %s,                %s,             %s,      %s,      %s,    %s,  %s,          %s,         %s,     %s,    %s,         %s,      %s,            %s,        %s,          %s,                  %s,                %s,             %s,            %s,     %s)"""
  #-----------------------------------------------------------------------------------------------------------------
  def additionalCreationProcedures(self, databaseCursor):
    pass
  #-----------------------------------------------------------------------------------------------------------------
  def partitionCreationParameters(self, uniqueIdentifier):
    startDate, endDate = uniqueIdentifier
    startDateAsString = "%4d-%02d-%02d" % startDate.timetuple()[:3]
    compressedStartDateAsString = startDateAsString.replace("-", "")
    endDateAsString = "%4d-%02d-%02d" % endDate.timetuple()[:3]
    return { "partitionName": "reports_%s" % compressedStartDateAsString,
             "startDate": startDateAsString,
             "endDate": endDateAsString,
             "compressedStartDate": compressedStartDateAsString
           }
  #-----------------------------------------------------------------------------------------------------------------
  def alterColumnDefinitions(self, databaseCursor, tableName):
    columnNameTypeDictionary = socorro_pg.columnNameTypeDictionaryForTable(tableName, databaseCursor)
    #if 'user_comments' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s rename column comments to user_comments""" % tableName)
    #if 'client_crash_date' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s rename column date to client_crash_date""" % tableName)
    #if 'app_notes' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s ADD COLUMN app_notes character varying(1024)""" % tableName)
    #if 'distributor' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s ADD COLUMN distributor character varying(20)""" % tableName)
    #if 'distributor_version' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s ADD COLUMN distributor_version character varying(20)""" % tableName)
    #if 'message' in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s rename column message to processor_notes""" % tableName)
    #if 'started_datetime' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s rename column starteddatetime to started_datetime""" % tableName)
    #if 'completed_datetime' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s rename column completeddatetime to completed_datetime""" % tableName)
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    databaseCursor.execute("""DROP RULE IF EXISTS rule_reports_partition ON reports;""")
    self.updateColumnDefinitions(databaseCursor)
    indexesList = socorro_pg.indexesForTable(self.name, databaseCursor)
    #if 'reports_pkey' in indexesList:
      #databaseCursor.execute("""ALTER TABLE reports DROP CONSTRAINT reports_pkey CASCADE;""")
    #if 'idx_reports_date' in indexesList:
      #databaseCursor.execute("""DROP INDEX idx_reports_date;""")
    #if 'ix_reports_signature' in indexesList:
      #databaseCursor.execute("""DROP INDEX ix_reports_signature;""")
    #if 'ix_reports_url' in indexesList:
      #databaseCursor.execute("""DROP INDEX ix_reports_url;""")
    #if 'ix_reports_uuid' in indexesList:
      #databaseCursor.execute("""DROP INDEX ix_reports_uuid;""")
    #triggersList = socorro_pg.triggersForTable(self.name, databaseCursor)
    #if 'reports_insert_trigger' not in triggersList:
      #databaseCursor.execute("""CREATE TRIGGER reports_insert_trigger
                                    #BEFORE INSERT ON reports
                                    #FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""")
databaseDependenciesForSetup[ReportsTable] = []

#=================================================================================================================
class PriorityJobsTable(Table):
  """Define the table 'priorityjobs'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, name="priorityjobs", logger=None, **kwargs):
    super(PriorityJobsTable, self).__init__(name=name, logger=logger,
                                            creationSql = """
                                                CREATE TABLE %s (
                                                    uuid varchar(255) NOT NULL PRIMARY KEY
                                                );""" % name)
databaseDependenciesForSetup[PriorityJobsTable] = []

#=================================================================================================================
class ProcessorsTable(Table):
  """Define the table 'processors'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(ProcessorsTable, self).__init__(name = "processors", logger=logger,
                                        creationSql = """
                                            CREATE TABLE processors (
                                                id serial NOT NULL PRIMARY KEY,
                                                name varchar(255) NOT NULL UNIQUE,
                                                startdatetime timestamp without time zone NOT NULL,
                                                lastseendatetime timestamp without time zone
                                            );""")
  def updateDefinition(self, databaseCursor):
    indexesList = socorro_pg.indexesForTable(self.name, databaseCursor)
    #if 'idx_processor_name' in indexesList:
      #databaseCursor.execute("""DROP INDEX idx_processor_name;
                                #ALTER TABLE processors ADD CONSTRAINT processors_name_key UNIQUE (name);""")
databaseDependenciesForSetup[ProcessorsTable] = []

#=================================================================================================================
class JobsTable(Table):
  """Define the table 'jobs'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(JobsTable, self).__init__(name = "jobs",  logger=logger,
                                    creationSql = """
                                        CREATE TABLE jobs (
                                            id serial NOT NULL PRIMARY KEY,
                                            pathname character varying(1024) NOT NULL,
                                            uuid varchar(50) NOT NULL UNIQUE,
                                            owner integer,
                                            priority integer DEFAULT 0,
                                            queueddatetime timestamp without time zone,
                                            starteddatetime timestamp without time zone,
                                            completeddatetime timestamp without time zone,
                                            success boolean,
                                            message text,
                                            FOREIGN KEY (owner) REFERENCES processors (id)
                                        );
                                        CREATE INDEX jobs_owner_key ON jobs (owner);
                                        CREATE INDEX jobs_owner_starteddatetime_key ON jobs (owner, starteddatetime);
                                        CREATE INDEX jobs_owner_starteddatetime_priority_key ON jobs (owner, starteddatetime, priority DESC);
                                        CREATE INDEX jobs_completeddatetime_queueddatetime_key ON jobs (completeddatetime, queueddatetime);
                                        --CREATE INDEX jobs_priority_key ON jobs (priority);
                                        """)
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    indexesList = socorro_pg.indexesForTable(self.name, databaseCursor)
    if 'idx_owner' in indexesList:
      databaseCursor.execute("""
          DROP INDEX idx_owner;
          CREATE INDEX jobs_owner_key ON jobs (owner);""")
    if 'idx_queueddatetime' in indexesList:
      databaseCursor.execute("""
          DROP INDEX idx_queueddatetime;""")
    if 'idx_starteddatetime' in indexesList:
      databaseCursor.execute("""
          DROP INDEX idx_starteddatetime;""")
    if 'jobs_priority_queueddatetime' in indexesList:
      databaseCursor.execute("""
          DROP INDEX jobs_priority_queueddatetime;""")
    if 'jobs_owner_starteddatetime' in indexesList:
      databaseCursor.execute("""
          DROP INDEX jobs_owner_starteddatetime;
          CREATE INDEX jobs_owner_starteddatetime_key ON jobs (owner, starteddatetime);""")
    #if 'jobs_priority_key' not in indexesList:
    #  databaseCursor.execute("""CREATE INDEX jobs_priority_key ON jobs (priority);""")
    if 'jobs_owner_starteddatetime_priority_key' not in indexesList:
      databaseCursor.execute("""CREATE INDEX jobs_owner_starteddatetime_priority_key ON jobs (owner, starteddatetime, priority DESC);""")
    if 'jobs_completeddatetime_queueddatetime_key' not in indexesList:
      databaseCursor.execute("""CREATE INDEX jobs_completeddatetime_queueddatetime_key ON jobs (completeddatetime, queueddatetime);""")
    if 'jobs_success_key' not in indexesList:
      databaseCursor.execute("""CREATE INDEX jobs_success_key ON jobs (success);""")
databaseDependenciesForSetup[JobsTable] = [ProcessorsTable]

#=================================================================================================================
class BugsTable(Table):
  """Define the table 'bug_associations'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(BugsTable, self).__init__(name = "bugs", logger=logger,
                                        creationSql = """
                                            CREATE TABLE bugs (
                                                id int NOT NULL,
                                                status text,
                                                resolution text,
                                                short_desc text
                                            );
                                            ALTER TABLE ONLY bugs
                                                ADD CONSTRAINT bugs_pkey PRIMARY KEY (id);
                                            """)
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[BugsTable] = []

#=================================================================================================================
class BugAssociationsTable(Table):
  """Define the table 'bug_associations'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(BugAssociationsTable, self).__init__(name = "bug_associations", logger=logger,
                                        creationSql = """
                                            CREATE TABLE bug_associations (
                                                signature text NOT NULL,
                                                bug_id int NOT NULL
                                            );
                                            ALTER TABLE ONLY bug_associations
                                                ADD CONSTRAINT bug_associations_pkey PRIMARY KEY (signature, bug_id);
                                            CREATE INDEX idx_bug_associations_bug_id ON bug_associations (bug_id);
                                            ALTER TABLE bug_associations
                                                ADD CONSTRAINT bug_associations_bug_id_fkey FOREIGN KEY (bug_id) REFERENCES bugs(id) ON DELETE CASCADE;
                                            """)
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[BugAssociationsTable] = [BugsTable]


#=================================================================================================================
class ServerStatusTable(Table):
  """Define the table 'server_status'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(ServerStatusTable, self).__init__(name='server_status', logger=logger,
                                       creationSql="""
                                          CREATE TABLE server_status (
                                              id serial NOT NULL,
                                              date_recently_completed timestamp without time zone,
                                              date_oldest_job_queued timestamp without time zone,
                                              avg_process_sec real,
                                              avg_wait_sec real,
                                              waiting_job_count integer NOT NULL,
                                              processors_count integer NOT NULL,
                                              date_created timestamp without time zone NOT NULL
                                          );
                                          ALTER TABLE ONLY server_status
                                              ADD CONSTRAINT server_status_pkey PRIMARY KEY (id);
                                          CREATE INDEX idx_server_status_date ON server_status USING btree (date_created, id);
                                          """)
databaseDependenciesForSetup[ServerStatusTable] = []

# #=================================================================================================================
# class SignatureDimsTable(Table):
#   """Define the table 'signaturedims'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(SignatureDimsTable, self).__init__(name='signaturedims', logger=logger,
#                                        creationSql="""
#                                           CREATE TABLE signaturedims (
#                                               id serial NOT NULL,
#                                               signature character varying(255) NOT NULL);
#                                           ALTER TABLE ONLY signaturedims
#                                               ADD CONSTRAINT signaturedims_pkey PRIMARY KEY (id);
#                                           CREATE UNIQUE INDEX signaturedims_signature_key ON signaturedims USING btree (signature);
#                                           """)
# databaseDependenciesForSetup[SignatureDimsTable] = []

#=================================================================================================================
class ReleaseEnum(DatabaseObject):
  def __init__(self,logger, **kwargs):
    super(ReleaseEnum, self).__init__(name='release_enum', logger=logger,
                                      creationSql="CREATE TYPE release_enum AS ENUM ('major', 'milestone', 'development');"
                                      )
  def drop(self, databaseCursor):
    databaseCursor.execute("drop type if exists %s cascade"%self.name)
databaseDependenciesForSetup[ReleaseEnum] = []

#=================================================================================================================
class ProductDimsTable(Table):
  """Define the table 'productdims'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(ProductDimsTable, self).__init__(name='productdims', logger=logger,
                                       creationSql="""
                                          CREATE TABLE productdims (
                                              id serial NOT NULL PRIMARY KEY,
                                              product TEXT NOT NULL, -- varchar(30)
                                              version TEXT NOT NULL, -- varchar(16)
                                              branch TEXT NOT NULL, -- from branches table: 'gecko version'
                                              release release_enum -- 'major':x.y.z..., 'milestone':x.ypre, 'development':x.y[ab]z
                                          );
                                          CREATE UNIQUE INDEX productdims_product_version_key ON productdims (product, version);
                                          CREATE INDEX productdims_release_key ON productdims (release);
                                          """)
databaseDependenciesForSetup[ProductDimsTable] = [ReleaseEnum]

class BranchesView(DatabaseObject):
  """Define the view 'branches'"""
  def __init__(self, logger, **kwargs):
    super(BranchesView, self).__init__(name='branches', logger=logger,
                                       creationSql = """
                                         CREATE VIEW branches AS SELECT product,version,branch FROM productdims
                                         """)
databaseDependenciesForSetup[BranchesView] = [ProductDimsTable]

#=================================================================================================================
class UrlDimsTable(Table):
  """Define the table 'urldims'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(UrlDimsTable, self).__init__(name='urldims', logger=logger,
                                       creationSql="""
                                          CREATE TABLE urldims (
                                              id serial NOT NULL,
                                              domain character varying(255) NOT NULL,
                                              url character varying(255) NOT NULL);
                                          ALTER TABLE ONLY urldims
                                              ADD CONSTRAINT urldims_pkey PRIMARY KEY (id);
                                          CREATE UNIQUE INDEX urldims_url_domain_key ON urldims USING btree (url, domain);
                                          """)
databaseDependenciesForSetup[UrlDimsTable] = []

#=================================================================================================================
class OsDimsTable(Table):
  """Define the table osdims"""
  def __init__(self, logger, **kwargs):
    super(OsDimsTable,self).__init__(name='osdims', logger=logger,
                                     creationSql="""
                                        CREATE TABLE osdims (
                                          id serial NOT NULL PRIMARY KEY,
                                          os_name TEXT,
                                          os_version TEXT
                                        );
                                        CREATE UNIQUE INDEX osdims_name_version_key on osdims (os_name,os_version);
                                        """)
databaseDependenciesForSetup[OsDimsTable] = []

# #=================================================================================================================
# class CrashReportsTable(PartitionedTable):
#   """Define the table 'crash_reports'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(CrashReportsTable, self).__init__(name='crash_reports', logger=logger,
#                                         creationSql="""
#                                           CREATE TABLE crash_reports (
#                                               id serial NOT NULL,
#                                               uuid TEXT NOT NULL,
#                                               client_crash_date TIMESTAMP with time zone,
#                                               install_age INTEGER,
#                                               last_crash INTEGER,
#                                               uptime INTEGER,
#                                               cpu_name TEXT, -- varchar(100)
#                                               cpu_info TEXT, -- varchar(100)
#                                               reason TEXT,   -- varchar(255)
#                                               address TEXT,  -- varchar(20)
#                                               build_date TIMESTAMP without time zone,
#                                               started_datetime TIMESTAMP without time zone,
#                                               completed_datetime TIMESTAMP without time zone,
#                                               date_processed TIMESTAMP without time zone,
#                                               success BOOLEAN,
#                                               truncated BOOLEAN,
#                                               processor_notes TEXT,
#                                               user_comments TEXT,  -- varchar(1024)
#                                               app_notes TEXT,  -- varchar(1024)
#                                               distributor TEXT, -- varchar(20)
#                                               distributor_version TEXT, -- varchar(20)
#                                               signaturedims_id INTEGER,
#                                               productdims_id INTEGER,
#                                               osdims_id INTEGER,
#                                               urldims_id INTEGER,
#                                               FOREIGN KEY (signaturedims_id) REFERENCES signaturedims(id) ON DELETE CASCADE,
#                                               FOREIGN KEY (productdims_id) REFERENCES productdims(id) ON DELETE CASCADE,
#                                               FOREIGN KEY (osdims_id) REFERENCES osdims(id) ON DELETE CASCADE,
#                                               FOREIGN KEY (urldims_id) REFERENCES urldims(id) ON DELETE CASCADE
#                                           );
#                                           --CREATE TRIGGER reports_insert_trigger
#                                           --    BEFORE INSERT ON reports
#                                           --    FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""",
#                                        partitionCreationSqlTemplate="""
#                                           CREATE TABLE %(partitionName)s (
#                                               CONSTRAINT %(partitionName)s_date_check CHECK (TIMESTAMP without time zone '%(startDate)s' <= date_processed and date_processed < TIMESTAMP without time zone '%(endDate)s'),
#                                               CONSTRAINT %(partitionName)s_unique_uuid unique (uuid),
#                                               PRIMARY KEY(id)
#                                           )
#                                           INHERITS (crash_reports);
#                                           CREATE INDEX %(partitionName)s_date_processed_key ON %(partitionName)s (date_processed);
#                                           CREATE INDEX %(partitionName)s_client_crash_date_key ON %(partitionName)s (client_crash_date);
#                                           CREATE INDEX %(partitionName)s_uuid_key ON %(partitionName)s (uuid);
#                                           CREATE INDEX %(partitionName)s_signature_key ON %(partitionName)s (signaturedims_id);
#                                           CREATE INDEX %(partitionName)s_url_key ON %(partitionName)s (urldims_id);
#                                           CREATE INDEX %(partitionName)s_product_version_key ON %(partitionName)s (productdims_id);
#                                           --CREATE INDEX %(partitionName)s_uuid_date_processed_key ON %(partitionName)s (uuid, date_processed);
#                                           CREATE INDEX %(partitionName)s_signature_date_processed_key ON %(partitionName)s (signaturedims_id, date_processed);
#                                           """
#                                       )
#     self.columns =  ("uuid", "client_crash_date", "date_processed", "install_age", "last_crash", "uptime", "user_comments", "app_notes", "distributor", "distributor_version", "productdims_id", "urldims_id")
#     columnNames = ','.join(self.columns)
#     placeholders = ','.join(('%s' for x in self.columns))
#     self.insertSql = """insert into TABLENAME (%s) values (%s)"""%(columnNames,placeholders)

#   def partitionCreationParameters(self,uniqueIdentifier):
#     startDate, endDate = uniqueIdentifier
#     startDateAsString = "%4d-%02d-%02d" % startDate.timetuple()[:3]
#     compressedStartDateAsString = startDateAsString.replace("-", "")
#     endDateAsString = "%4d-%02d-%02d" % endDate.timetuple()[:3]
#     return { "partitionName": "crash_reports_%s" % compressedStartDateAsString,
#              "startDate": startDateAsString,
#              "endDate": endDateAsString,
#              "compressedStartDate": compressedStartDateAsString,
#            }

# databaseDependenciesForSetup[CrashReportsTable] = [SignatureDimsTable,ProductDimsTable,OsDimsTable,UrlDimsTable]

#=================================================================================================================
class ExtensionsTable(PartitionedTable):
  """Define the table 'extensions'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(ExtensionsTable, self).__init__(name='extensions', logger=logger,
                                          creationSql="""
                                              CREATE TABLE extensions (
                                                  report_id integer NOT NULL,
                                                  date_processed timestamp without time zone,
                                                  extension_key integer NOT NULL,
                                                  extension_id text NOT NULL,
                                                  extension_version text
                                              );
                                              --CREATE TRIGGER extensions_insert_trigger
                                              --    BEFORE INSERT ON extensions
                                              --    FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""",
                                          partitionCreationSqlTemplate="""
                                              CREATE TABLE %(partitionName)s (
                                                  CONSTRAINT %(partitionName)s_date_check CHECK (TIMESTAMP without time zone '%(startDate)s' <= date_processed and date_processed < TIMESTAMP without time zone '%(endDate)s'),
                                                  PRIMARY KEY (report_id, extension_key)
                                                  )
                                                  INHERITS (extensions);
                                              CREATE INDEX %(partitionName)s_report_id_date_key ON %(partitionName)s (report_id, date_processed, extension_key);
                                              CREATE INDEX %(partitionName)s_extension_id_extension_version_idx ON %(partitionName)s (extension_id, extension_version);
                                              ALTER TABLE %(partitionName)s
                                                  ADD CONSTRAINT %(partitionName)s_report_id_fkey FOREIGN KEY (report_id) REFERENCES reports_%(compressedStartDate)s(id) ON DELETE CASCADE;
                                              """)
    self.insertSql = """insert into TABLENAME (report_id, date_processed, extension_key, extension_id, extension_version) values (%s, %s, %s, %s, %s)"""
  #-----------------------------------------------------------------------------------------------------------------
  def alterColumnDefinitions(self, databaseCursor, tableName):
    columnNameTypeDictionary = socorro_pg.columnNameTypeDictionaryForTable(tableName, databaseCursor)
    #if 'date_processed' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s
                                    #ADD COLUMN date_processed TIMESTAMP without time zone;""" % tableName)
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    self.updateColumnDefinitions(databaseCursor)
    indexesList = socorro_pg.indexesForTable(self.name, databaseCursor)
    #if 'extensions_pkey' in indexesList:
      #databaseCursor.execute("""ALTER TABLE extensions
                                    #DROP CONSTRAINT extensions_pkey;""")
    #databaseCursor.execute("""DROP RULE IF EXISTS rule_extensions_partition ON extensions;""")
    #triggersList = socorro_pg.triggersForTable(self.name, databaseCursor)
    #if 'extensions_insert_trigger' not in triggersList:
      #databaseCursor.execute("""CREATE TRIGGER extensions_insert_trigger
                                    #BEFORE INSERT ON extensions
                                    #FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""")
  #-----------------------------------------------------------------------------------------------------------------
  def partitionCreationParameters(self, uniqueIdentifier):
    startDate, endDate = uniqueIdentifier
    startDateAsString = "%4d-%02d-%02d" % startDate.timetuple()[:3]
    compressedStartDateAsString = startDateAsString.replace("-", "")
    endDateAsString = "%4d-%02d-%02d" % endDate.timetuple()[:3]
    return { "partitionName": "extensions_%s" % compressedStartDateAsString,
             "startDate": startDateAsString,
             "endDate": endDateAsString,
             "compressedStartDate": compressedStartDateAsString
           }
databaseDependenciesForPartition[ExtensionsTable] = [ReportsTable]
databaseDependenciesForSetup[ExtensionsTable] = []

#=================================================================================================================
class FramesTable(PartitionedTable):
  """Define the table 'frames'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(FramesTable, self).__init__(name='frames', logger=logger,
                                      creationSql="""
                                          CREATE TABLE frames (
                                              report_id integer NOT NULL,
                                              date_processed timestamp without time zone,
                                              frame_num integer NOT NULL,
                                              signature varchar(255)
                                          );
                                          --CREATE TRIGGER frames_insert_trigger
                                          --    BEFORE INSERT ON frames
                                          --    FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""",
                                      partitionCreationSqlTemplate="""
                                          CREATE TABLE %(partitionName)s (
                                              CONSTRAINT %(partitionName)s_date_check CHECK (TIMESTAMP without time zone '%(startDate)s' <= date_processed and date_processed < TIMESTAMP without time zone '%(endDate)s'),
                                              PRIMARY KEY (report_id, frame_num)
                                          )
                                          INHERITS (frames);
                                          CREATE INDEX %(partitionName)s_report_id_date_key ON %(partitionName)s (report_id, date_processed);
                                          ALTER TABLE %(partitionName)s
                                              ADD CONSTRAINT %(partitionName)s_report_id_fkey FOREIGN KEY (report_id) REFERENCES reports_%(compressedStartDate)s(id) ON DELETE CASCADE;
                                          """
                                     )
    self.insertSql = """insert into TABLENAME (report_id, frame_num, date_processed, signature) values (%s, %s, %s, %s)"""
  #-----------------------------------------------------------------------------------------------------------------
  def alterColumnDefinitions(self, databaseCursor, tableName):
    columnNameTypeDictionary = socorro_pg.columnNameTypeDictionaryForTable(tableName, databaseCursor)
    #if 'date_processed' not in columnNameTypeDictionary:
      #databaseCursor.execute("""ALTER TABLE %s
                                    #ADD COLUMN date_processed TIMESTAMP without time zone;""" % tableName)
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    self.updateColumnDefinitions(databaseCursor)
    indexesList = socorro_pg.indexesForTable(self.name, databaseCursor)
    #if 'frames_pkey' in indexesList:
      #databaseCursor.execute("""ALTER TABLE frames
                                    #DROP CONSTRAINT frames_pkey;""")
    #databaseCursor.execute("""DROP RULE IF EXISTS rule_frames_partition ON frames;""")
    #triggersList = socorro_pg.triggersForTable(self.name, databaseCursor)
    #if 'frames_insert_trigger' not in triggersList:
      #databaseCursor.execute("""CREATE TRIGGER frames_insert_trigger
                                    #BEFORE INSERT ON frames
                                    #FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""")
  #-----------------------------------------------------------------------------------------------------------------
  def partitionCreationParameters(self, uniqueIdentifier):
    startDate, endDate = uniqueIdentifier
    startDateAsString = "%4d-%02d-%02d" % startDate.timetuple()[:3]
    compressedStartDateAsString = startDateAsString.replace("-", "")
    endDateAsString = "%4d-%02d-%02d" % endDate.timetuple()[:3]
    return { "partitionName": "frames_%s" % compressedStartDateAsString,
             "startDate": startDateAsString,
             "endDate": endDateAsString,
             "compressedStartDate": compressedStartDateAsString
           }
databaseDependenciesForPartition[FramesTable] = [ReportsTable]
databaseDependenciesForSetup[FramesTable] = []

#=================================================================================================================
# class DumpsTable(PartitionedTable):
#   """Define the table 'dumps'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(DumpsTable, self).__init__(name='dumps', logger=logger,
#                                      creationSql="""
#                                          CREATE TABLE dumps (
#                                              report_id integer NOT NULL PRIMARY KEY,
#                                              date_processed timestamp without time zone,
#                                              data text
#                                          );
#                                          --CREATE TRIGGER dumps_insert_trigger
#                                          --   BEFORE INSERT ON dumps
#                                          --   FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""",
#                                      partitionCreationSqlTemplate="""
#                                          CREATE TABLE %(partitionName)s (
#                                              CONSTRAINT %(partitionName)s_date_check CHECK (TIMESTAMP without time zone '%(startDate)s' <= date_processed and date_processed < TIMESTAMP without time zone '%(endDate)s')
#                                          )
#                                          INHERITS (dumps);
#                                          CREATE INDEX %(partitionName)s_report_id_date_key ON %(partitionName)s (report_id, date_processed);
#                                          ALTER TABLE %(partitionName)s
#                                              ADD CONSTRAINT %(partitionName)s_report_id_fkey FOREIGN KEY (report_id) REFERENCES crash_reports_%(compressedStartDate)s(id) ON DELETE CASCADE;
#                                          """)
#     self.insertSql = """insert into TABLENAME (report_id, date_processed, data) values (%s, %s, %s)"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def alterColumnDefinitions(self, databaseCursor, tableName):
#     columnNameTypeDictionary = socorro_pg.columnNameTypeDictionaryForTable(tableName, databaseCursor)
#     #if 'date_processed' not in columnNameTypeDictionary:
#       #databaseCursor.execute("""ALTER TABLE %s
#                                     #ADD COLUMN date_processed TIMESTAMP without time zone;""" % tableName)
#   #-----------------------------------------------------------------------------------------------------------------
#   def updateDefinition(self, databaseCursor):
#     self.updateColumnDefinitions(databaseCursor)
#     indexesList = socorro_pg.indexesForTable(self.name, databaseCursor)
#     #if 'dumps_pkey' in indexesList:
#       #databaseCursor.execute("""ALTER TABLE dumps
#                                     #DROP CONSTRAINT dumps_pkey;""")
#     #databaseCursor.execute("""DROP RULE IF EXISTS rule_dumps_partition ON dumps;""")
#     #triggersList = socorro_pg.triggersForTable(self.name, databaseCursor)
#     #if 'dumps_insert_trigger' not in triggersList:
#       #databaseCursor.execute("""CREATE TRIGGER dumps_insert_trigger
#                                     #BEFORE INSERT ON dumps
#                                     #FOR EACH ROW EXECUTE PROCEDURE partition_insert_trigger();""")
#   #-----------------------------------------------------------------------------------------------------------------
#   def partitionCreationParameters(self, uniqueIdentifier):
#     startDate, endDate = uniqueIdentifier
#     startDateAsString = "%4d-%02d-%02d" % startDate.timetuple()[:3]
#     compressedStartDateAsString = startDateAsString.replace("-", "")
#     endDateAsString = "%4d-%02d-%02d" % endDate.timetuple()[:3]
#     return { "partitionName": "dumps_%s" % compressedStartDateAsString,
#              "startDate": startDateAsString,
#              "endDate": endDateAsString,
#              "compressedStartDate": compressedStartDateAsString
#            }
# databaseDependenciesForSetup[DumpsTable] = []
# databaseDependenciesForPartition[DumpsTable] = [CrashReportsTable]

#=================================================================================================================
class TimeBeforeFailureTable(Table):
  """Define the table 'time_before_failure'"""
  def __init__ (self, logger, **kwargs):
    super(TimeBeforeFailureTable, self).__init__(name='time_before_failure', logger=logger,
                                        creationSql="""
                                          CREATE TABLE time_before_failure (
                                              id serial NOT NULL PRIMARY KEY,
                                              sum_uptime_seconds float NOT NULL, -- integer is too small
                                              report_count integer NOT NULL,
                                              productdims_id integer,
                                              osdims_id integer,
                                              window_end TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                                              window_size INTERVAL NOT NULL
                                              );
                                          CREATE INDEX time_before_failure_window_end_window_size_key ON time_before_failure (window_end,window_size);
                                          CREATE INDEX time_before_failure_product_id_key ON time_before_failure (productdims_id);
                                          CREATE INDEX time_before_failure_os_id_key ON time_before_failure (osdims_id);
                                          ALTER TABLE ONLY time_before_failure
                                              ADD CONSTRAINT time_before_failure_productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id) ON DELETE CASCADE;
                                          ALTER TABLE ONLY time_before_failure
                                              ADD CONSTRAINT time_before_failure_osdims_id_fkey FOREIGN KEY (osdims_id) REFERENCES osdims(id) ON DELETE CASCADE;
                                              """)
databaseDependenciesForSetup[TimeBeforeFailureTable] = [ProductDimsTable, OsDimsTable]

# #=================================================================================================================
# class MTBFFactsTable(Table):
#   """Define the table 'mtbffacts'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(MTBFFactsTable, self).__init__(name='mtbffacts', logger=logger,
#                                        creationSql="""
#                                           CREATE TABLE mtbffacts (
#                                               id serial NOT NULL,
#                                               avg_seconds integer NOT NULL,
#                                               report_count integer NOT NULL,
#                                               unique_users integer NOT NULL,
#                                               day date,
#                                               productdims_id integer,
#                                               );
#                                           CREATE INDEX mtbffacts_day_key ON mtbffacts USING btree (day);
#                                           CREATE INDEX mtbffacts_product_id_key ON mtbffacts USING btree (productdims_id);
#                                           ALTER TABLE ONLY mtbffacts
#                                               ADD CONSTRAINT mtbffacts_pkey PRIMARY KEY (id);
#                                           ALTER TABLE ONLY mtbffacts
#                                               ADD CONSTRAINT mtbffacts_productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id);
#                                           """)
# databaseDependenciesForSetup[MTBFFactsTable] = [ProductDimsTable]

#=================================================================================================================
class ProductVisibilityTable(Table):
  """Define the table product_visibiilty: Used to decide what products are to be aggregated"""
  def __init__(self,logger,**kwargs):
    super(ProductVisibilityTable,self).__init__(name='product_visibility', logger=logger,
                               creationSql = """
                                 CREATE TABLE product_visibility (
                                   productdims_id integer NOT NULL PRIMARY KEY,
                                   start_date timestamp, -- set this manually for all mat views
                                   end_date timestamp,   -- set this manually: Used by mat views that care
                                   ignore boolean default False, -- force aggregation off for this product id
                                   featured boolean default false, -- if true, feature version on product dashboard
                                   throttle numeric(5,2) default 0.00 -- set this manually; used by active daily user reports
                                   );
                                   CREATE INDEX product_visibility_end_date ON product_visibility (end_date);
                                   CREATE INDEX product_visibility_start_date on product_visibility (start_date);
                                   ALTER TABLE ONLY product_visibility
                                     ADD CONSTRAINT product_visibility_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id) ON DELETE CASCADE;
                                 """
                                           )
databaseDependenciesForSetup[ProductVisibilityTable] = [ProductDimsTable]
# #=================================================================================================================
# class MTBFConfigTable(Table):
#   """Define the table 'mtbfconfig'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(Table, self).__init__(name='mtbfconfig', logger=logger,
#                                        creationSql="""
#                                           CREATE TABLE mtbfconfig (
#                                               id serial NOT NULL,
#                                               productdims_id integer,
#                                               osdims_id integer,
#                                               start_dt date,
#                                               end_dt date);
#                                           ALTER TABLE ONLY mtbfconfig
#                                               ADD CONSTRAINT mtbfconfig_pkey PRIMARY KEY (id);
#                                           CREATE INDEX mtbfconfig_end_dt_key ON mtbfconfig USING btree (end_dt);
#                                           CREATE INDEX mtbfconfig_start_dt_key ON mtbfconfig USING btree (start_dt);
#                                           ALTER TABLE ONLY mtbfconfig
#                                               ADD CONSTRAINT mtbfconfig_productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id);
#                                           ALTER TABLE ONLY mtbfconfig
#                                               ADD CONSTRAINT mtbfconfig_osdims_id_fkey FOREIGN KEY (osdims_id) REFERENCES osdims(id);
#                                           """)
# databaseDependenciesForSetup[MTBFConfigTable] = [ProductDimsTable, OsDimsTable]

#=================================================================================================================
# class TCByUrlConfigTable(Table):
#   """Define the table 'tcbyurlconfig'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(Table, self).__init__(name='tcbyurlconfig', logger=logger,
#                                        creationSql="""
#                                           CREATE TABLE tcbyurlconfig (
#                                               id serial NOT NULL,
#                                               productdims_id integer,
#                                               osdims_id integer,
#                                               enabled boolean);
#                                           ALTER TABLE ONLY tcbyurlconfig
#                                               ADD CONSTRAINT tcbyurlconfig_pkey PRIMARY KEY (id);
#                                           ALTER TABLE ONLY tcbyurlconfig
#                                               ADD CONSTRAINT tcbyurlconfig_productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id);
#                                           ALTER TABLE ONLY tcbyurlconfig
#                                               ADD CONSTRAINT tcbyurlconfig_osdims_id_fkey FOREIGN KEY (osdims_id) REFERENCES osdims(id);
#                                           """)
# databaseDependenciesForSetup[TCByUrlConfigTable] = [ProductDimsTable, OsDimsTable]

# #=================================================================================================================
# class TCBySignatureConfigTable(Table):
#   """Define the table tcbysignatureconfig"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(Table, self).__init__(name='tcbysignatureconfig', logger = logger,
#                                 creationSql="""
#                                   CREATE TABLE tcbysignatureconfig (
#                                       id serial NOT NULL PRIMARY KEY,
#                                       productdims_id integer,
#                                       osdims_id integer,
#                                       start_dt date,
#                                       end_dt date);
#                                   ALTER TABLE tcbysignatureconfig
#                                       ADD CONSTRAINT tcbysignatureconfig_productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id);
#                                   ALTER TABLE tcbysignatureconfig
#                                       ADD CONSTRAINT tcbysignatureconfig_osdims_id_fkey FOREIGN KEY (osdims_id) REFERENCES osdims(id);
#                                   CREATE INDEX tcbysignatureconfig_dates ON tcbysignatureconfig (start_dt,end_dt)
#                                   """)
# databaseDependenciesForSetup[TCBySignatureConfigTable] = [ProductDimsTable, OsDimsTable]

#=================================================================================================================
class TopCrashesByUrlTable(Table):
  """Define the table 'top_crashes_by_url'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(TopCrashesByUrlTable, self).__init__(name='top_crashes_by_url', logger=logger,
                                creationSql="""
                                  CREATE TABLE top_crashes_by_url (
                                  id serial NOT NULL PRIMARY KEY,
                                  count integer NOT NULL,
                                  urldims_id integer,
                                  productdims_id integer,
                                  osdims_id integer,
                                  window_end TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                                  window_size INTERVAL NOT NULL
                                  );
                                  ALTER TABLE ONLY top_crashes_by_url
                                   ADD CONSTRAINT top_crashes_by_url_productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id) ON DELETE CASCADE;
                                  ALTER TABLE ONLY top_crashes_by_url
                                   ADD CONSTRAINT top_crashes_by_url_osdims_id_fkey FOREIGN KEY (osdims_id) REFERENCES osdims(id) ON DELETE CASCADE;
                                  ALTER TABLE ONLY top_crashes_by_url
                                   ADD CONSTRAINT top_crashes_by_url_urldims_id_fkey FOREIGN KEY (urldims_id) REFERENCES urldims(id) ON DELETE CASCADE;
                                  CREATE INDEX top_crashes_by_url_count_key ON top_crashes_by_url USING btree (count);
                                  CREATE INDEX top_crashes_by_url_window_end_window_size_key ON top_crashes_by_url USING btree (window_end,window_size);
                                  CREATE INDEX top_crashes_by_url_productdims_key ON top_crashes_by_url USING btree (productdims_id);
                                  CREATE INDEX top_crashes_by_url_urldims_key ON top_crashes_by_url USING btree (urldims_id);
                                  CREATE INDEX top_crashes_by_url_osdims_key ON top_crashes_by_url USING btree (osdims_id);

                                  """)
databaseDependenciesForSetup[TopCrashesByUrlTable] = [ProductDimsTable,OsDimsTable,UrlDimsTable]

#=================================================================================================================
class TopCrashByUrlSignatureTable(Table):
  """Define the table top_crashes_by_url_signature"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(TopCrashByUrlSignatureTable, self).__init__(name='top_crashes_by_url_signature', logger=logger,
                                creationSql="""
                                CREATE TABLE top_crashes_by_url_signature (
                                top_crashes_by_url_id integer NOT NULL, -- foreign key
                                signature TEXT NOT NULL,
                                count integer NOT NULL,
                                CONSTRAINT top_crashes_by_url_signature_fkey FOREIGN KEY (top_crashes_by_url_id) REFERENCES top_crashes_by_url(id) ON DELETE CASCADE,
                                CONSTRAINT top_crashes_by_url_signature_pkey PRIMARY KEY(top_crashes_by_url_id, signature)
                                );
                                """)
databaseDependenciesForSetup[TopCrashByUrlSignatureTable] = [TopCrashesByUrlTable]
#=================================================================================================================
class TopCrashUrlFactsReportsTable(Table):
  """Define the table 'topcrashurlfactsreports'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(TopCrashUrlFactsReportsTable, self).__init__(name='topcrashurlfactsreports', logger=logger,
                                       creationSql="""
                                          CREATE TABLE topcrashurlfactsreports (
                                              id serial NOT NULL,
                                              uuid character varying(50) NOT NULL,
                                              comments TEXT,
                                              topcrashurlfacts_id integer);
                                          ALTER TABLE ONLY topcrashurlfactsreports
                                              ADD CONSTRAINT topcrashurlfactsreports_pkey PRIMARY KEY (id);
                                          CREATE INDEX topcrashurlfactsreports_topcrashurlfacts_id_key ON topcrashurlfactsreports USING btree (topcrashurlfacts_id);
                                          ALTER TABLE ONLY topcrashurlfactsreports
                                              ADD CONSTRAINT topcrashurlfactsreports_topcrashurlfacts_id_fkey FOREIGN KEY (topcrashurlfacts_id) REFERENCES top_crashes_by_url(id) ON DELETE CASCADE;

                                          """)
databaseDependenciesForSetup[TopCrashUrlFactsReportsTable] = [TopCrashesByUrlTable]

#=================================================================================================================
class TopCrashesBySignatureTable(Table):
  """Define the table top_crashes_by_signature"""
  def __init__(self, logger, **kwargs):
    super(TopCrashesBySignatureTable, self).__init__(name='top_crashes_by_signature', logger=logger,
                                             creationSql= """
                                             CREATE TABLE top_crashes_by_signature (
                                               id serial NOT NULL PRIMARY KEY,
                                               count integer NOT NULL DEFAULT 0,
                                               uptime real DEFAULT 0.0,
                                               signature TEXT,
                                               productdims_id integer,
                                               osdims_id integer,
                                               hang_count integer DEFAULT 0,
                                               plugin_count integer DEFAULT 0,
                                               window_end TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                                               window_size INTERVAL NOT NULL
                                             );
                                             ALTER TABLE ONLY top_crashes_by_signature ADD CONSTRAINT productdims_id_fkey FOREIGN KEY (productdims_id) REFERENCES productdims(id) ON DELETE CASCADE;
                                             ALTER TABLE ONLY top_crashes_by_signature ADD CONSTRAINT osdims_id_fkey FOREIGN KEY (osdims_id) REFERENCES osdims(id) ON DELETE CASCADE;
                                             CREATE INDEX top_crashes_by_signature_productdims_window_end_idx ON top_crashes_by_signature (productdims_id, window_end DESC);
                                             CREATE INDEX top_crashes_by_signature_osdims_key ON top_crashes_by_signature (osdims_id);
                                             CREATE INDEX top_crashes_by_signature_signature_key ON top_crashes_by_signature (signature);
                                             CREATE INDEX top_crashes_by_signature_window_end_productdims_id_idx on top_crashes_by_signature (window_end desc, productdims_id);
                                            """
                                            )
databaseDependenciesForSetup[TopCrashesBySignatureTable] = [OsDimsTable,ProductDimsTable]

#=================================================================================================================
class PluginsTable(Table):
  """Define the table 'plugins'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, name="plugins", logger=None, **kwargs):
    super(PluginsTable, self).__init__(name=name, logger=logger,
                                            creationSql = """
                                                CREATE TABLE %s (
                                                    id SERIAL NOT NULL,
                                                    filename TEXT NOT NULL,
                                                    name TEXT NOT NULL,
                                                    PRIMARY KEY (id),
                                                    CONSTRAINT filename_name_key UNIQUE (filename, name)
                                                );""" % name)

  def insert(self, databaseCursor, rowTuple=None):
    databaseCursor.execute("insert into plugins (filename, name) values (%s, %s)", rowTuple)

databaseDependenciesForSetup[PluginsTable] = []

#=================================================================================================================
class PluginsReportsTable(PartitionedTable):
  """Define the table 'plugins_reports'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(PluginsReportsTable, self).__init__(name='plugins_reports', logger=logger,
                                          creationSql="""
                                              CREATE TABLE plugins_reports (
                                                  report_id integer NOT NULL,
                                                  plugin_id integer NOT NULL,
                                                  date_processed timestamp without time zone,
                                                  version TEXT NOT NULL
                                              );""",

                                          partitionCreationSqlTemplate="""
                                              CREATE TABLE %(partitionName)s (
                                                  CONSTRAINT %(partitionName)s_date_check CHECK (TIMESTAMP without time zone '%(startDate)s' <= date_processed and date_processed < TIMESTAMP without time zone '%(endDate)s'),
                                                  PRIMARY KEY (report_id, plugin_id)
                                                  )
                                                  INHERITS (plugins_reports);
                                              CREATE INDEX %(partitionName)s_report_id_date_key ON %(partitionName)s (report_id, date_processed, plugin_id);
                                              ALTER TABLE %(partitionName)s
                                                  ADD CONSTRAINT %(partitionName)s_report_id_fkey FOREIGN KEY (report_id) REFERENCES reports_%(compressedStartDate)s(id) ON DELETE CASCADE;
                                              ALTER TABLE %(partitionName)s
                                                  ADD CONSTRAINT %(partitionName)s_plugin_id_fkey FOREIGN KEY (plugin_id) REFERENCES plugins(id) ON DELETE CASCADE;
                                              """)
    self.insertSql = """insert into TABLENAME (report_id, plugin_id, date_processed, version) values
                                              (%s, (select id from plugins where filename = %s and name = %s),
                                              %s, %s)"""
  #-----------------------------------------------------------------------------------------------------------------
  def partitionCreationParameters(self, uniqueIdentifier):
    startDate, endDate = uniqueIdentifier
    startDateAsString = "%4d-%02d-%02d" % startDate.timetuple()[:3]
    compressedStartDateAsString = startDateAsString.replace("-", "")
    endDateAsString = "%4d-%02d-%02d" % endDate.timetuple()[:3]
    return { "partitionName": "plugins_reports_%s" % compressedStartDateAsString,
             "startDate": startDateAsString,
             "endDate": endDateAsString,
             "compressedStartDate": compressedStartDateAsString
           }
databaseDependenciesForPartition[PluginsReportsTable] = [ReportsTable]
databaseDependenciesForSetup[PluginsReportsTable] = [PluginsTable]

class AlexaTopsitesTable(Table):
  """Define the table 'alexa_topsites'"""
  def __init__(self, logger, **kwargs):
    super(AlexaTopsitesTable,self).__init__(name='alexa_topsites',logger=logger,
                                            creationSql = """
                                              CREATE TABLE alexa_topsites (
                                                domain text NOT NULL PRIMARY KEY,
                                                rank integer DEFAULT 10000,
                                                last_updated timestamp without time zone
                                                );
                                              """
                                            )
databaseDependenciesForSetup[AlexaTopsitesTable] = []

class RawAduTable(Table):
  """Define the table raw_adu"""
  def __init__(self, logger, **kwargs):
    super(RawAduTable,self).__init__(name='raw_adu', logger=logger,
                                     creationSql = """
                                       CREATE TABLE raw_adu (
                                         adu_count integer,
                                         date timestamp without time zone,
                                         product_name text,
                                         product_os_platform text,
                                         product_os_version text,
                                         product_version text
                                         );
                                         CREATE INDEX raw_adu_1_idx ON raw_adu (date,
                                         product_name,
                                         product_version,
                                         product_os_platform,
                                         product_os_version);
                                       """
                                    )
databaseDependenciesForSetup[RawAduTable] = []


#=================================================================================================================
class BuildsTable(Table):
  """Define the table 'builds'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(BuildsTable, self).__init__(name = "builds", logger=logger,
                                        creationSql = """
                                            CREATE TABLE builds (
                                                product text,
                                                version text,
                                                platform text,
                                                buildid BIGINT,
                                                changeset text,
                                                filename text,
                                                date timestamp without time zone default now(),
                                            CONSTRAINT builds_key UNIQUE (product, version, platform, buildid));
                                        """)
    self.insertSql = """INSERT INTO TABLENAME (product, version, platform, buildid, changeset, filename, date) values (%s, %s, %s, %s, %s, %s, %s)"""

  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[BuildsTable] = []

#=================================================================================================================
class DailyCrashesTable(Table):
  """Define the table 'daily_crashes'
     Notes:
        report_type - single character code 'C' or 'H' for  Crash or Hang
        os_short_name - Cron does inserts directly into this table for 'Win, Mac, Lin'.
            This is a different processes than the osdims table, so osdims is not used.

        adu_day - These values are time shifted to match raw_adu. Example: 2010-05-24T00:00:00
  """
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(DailyCrashesTable, self).__init__(name = "daily_crashes", logger=logger,
                                        creationSql = """
                                            CREATE TABLE daily_crashes (
                                                id serial NOT NULL PRIMARY KEY,
                                                count INTEGER DEFAULT 0 NOT NULL,
                                                report_type CHAR(1) NOT NULL DEFAULT 'C',
                                                productdims_id INTEGER REFERENCES productdims(id),
                                                os_short_name CHAR(3),
                                                adu_day TIMESTAMP WITHOUT TIME ZONE NOT NULL,
                                                CONSTRAINT day_product_os_report_type_unique UNIQUE (adu_day, productdims_id, os_short_name, report_type));
                                        """)
    self.insertSql = """INSERT INTO TABLENAME (count, report_type, productdims_id, os_short_name, adu_day) values (%s, %s, %s, %s, %s)"""

  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[DailyCrashesTable] = []



# #=================================================================================================================
# class TopCrashersTable(Table):
#   """Define the table 'topcrashers'"""
#   #-----------------------------------------------------------------------------------------------------------------
#   def __init__ (self, logger, **kwargs):
#     super(TopCrashersTable, self).__init__(name='topcrashers', logger=logger,
#                                        creationSql="""
#                                           CREATE TABLE topcrashers (
#                                               id serial NOT NULL,
#                                               signature character varying(255) NOT NULL,
#                                               version character varying(30) NOT NULL,
#                                               product character varying(30) NOT NULL,
#                                               build character varying(30) NOT NULL,
#                                               total integer,
#                                               win integer,
#                                               mac integer,
#                                               linux integer,
#                                               rank integer,
#                                               last_rank integer,
#                                               trend character varying(30),
#                                               uptime real,
#                                               users integer,
#                                               last_updated timestamp without time zone
#                                           );
#                                           ALTER TABLE ONLY topcrashers
#                                               ADD CONSTRAINT topcrashers_pkey PRIMARY KEY (id);
#                                           """)
# databaseDependenciesForSetup[TopCrashersTable] = []

#=================================================================================================================
#class ParititioningTriggerScript(DatabaseObject):
  ##-----------------------------------------------------------------------------------------------------------------
  #def __init__ (self, logger):
    #super(ParititioningTriggerScript, self).__init__(name = "partition_insert_trigger", logger=logger,
                                                     #creationSql = """
#CREATE OR REPLACE FUNCTION partition_insert_trigger()
#RETURNS TRIGGER AS $$
#import socorro.database.server as ds
#try:
  #targetTableName = ds.targetTableName(TD["table_name"], TD['new']['date_processed'])
  ##plpy.info(targetTableName)
  #planName = ds.targetTableInsertPlanName (targetTableName)
  ##plpy.info("using plan: %s" % planName)
  #values = ds.getValuesList(TD, SD, plpy)
  ##plpy.info(str(values))
  ##plpy.info('about to execute plan')
  #result = plpy.execute(SD[planName], values)
  #return None
#except KeyError:  #no plan
  ##plpy.info("oops no plan for: %s" % planName)
  #SD[planName] = ds.createNewInsertQueryPlan(TD, SD, targetTableName, planName, plpy)
  ##plpy.info('about to execute plan for second time')
  #result = plpy.execute(SD[planName], values)
  #return None
#$$
#LANGUAGE plpythonu;""")
  #def updateDefinition(self, databaseCursor):
    #databaseCursor.execute(self.creationSql)

#=================================================================================================================
#class ChattyParititioningTriggerScript(DatabaseObject):
  #-----------------------------------------------------------------------------------------------------------------
  #def __init__ (self, logger):
    #super(ChattyParititioningTriggerScript, self).__init__(name = "partition_insert_trigger", logger=logger,
                                                     #creationSql = """
#CREATE OR REPLACE FUNCTION partition_insert_trigger()
#RETURNS TRIGGER AS $$
#import socorro.database.server as ds
#import logging
#import logging.handlers
#try:
  #targetTableName = ds.targetTableName(TD["table_name"], TD['new']['date_processed'])
  #planName = ds.targetTableInsertPlanName (targetTableName)
  #try:
    #logger = SD["logger"]
  #except KeyError:
    #SD["logger"] = logger = logging.getLogger(targetTableName)
    #logger.setLevel(logging.DEBUG)
    #rotatingFileLog = logging.handlers.RotatingFileHandler("/tmp/partitionTrigger.log", "a", 100000000, 10)
    #rotatingFileLog.setLevel(logging.DEBUG)
    #rotatingFileLogFormatter = logging.Formatter('%(asctime)s %(levelname)s - %(message)s')
    #rotatingFileLog.setFormatter(rotatingFileLogFormatter)
    #logger.addHandler(rotatingFileLog)
    #logger.debug("---------- beginning new session ----------")
    #SD["counter"] = 0
  #values = ds.getValuesList(TD, SD, plpy)
  #logger.debug("%08d plan: %s", SD["counter"], planName)
  #SD["counter"] += 1
  #result = plpy.execute(SD[planName], values)
  #return 'SKIP'
#except KeyError:  #no plan
  #logger.debug('creating new plan for: %s', planName)
  #SD[planName] = ds.createNewInsertQueryPlan(TD, SD, targetTableName, planName, plpy)
  #result = plpy.execute(SD[planName], values)
  #return 'SKIP'
#$$
#LANGUAGE plpythonu;""")
  ##-----------------------------------------------------------------------------------------------------------------
  #def updateDefinition(self, databaseCursor):
    #databaseCursor.execute(self.creationSql)


#=================================================================================================================
class EmailCampaignsTable(Table):
  """Define the table 'email_campaigns'
     Notes: * email_count is populated after the record is inserted (TBD)
            * product/versions is denormalized to record versions used, but isn't searchable
  """
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(EmailCampaignsTable, self).__init__(name = "email_campaigns", logger=logger,
                                        creationSql = """
                                            CREATE TABLE email_campaigns (
                                                id serial NOT NULL PRIMARY KEY,
                                                product TEXT NOT NULL,
                                                versions TEXT NOT NULL,                                                
                                                signature TEXT NOT NULL,
                                                subject TEXT NOT NULL,
                                                body TEXT NOT NULL,
                                                start_date timestamp without time zone NOT NULL,
                                                end_date timestamp without time zone NOT NULL,
                                                email_count INTEGER DEFAULT 0,
                                                author TEXT NOT NULL,
                                                date_created timestamp without time zone NOT NULL DEFAULT now());
                                            CREATE INDEX email_campaigns_product_signature_key ON email_campaigns (product, signature);
                                        """)
    self.insertSql = """INSERT INTO email_campaigns (product, versions, signature, subject, body, start_date, end_date, email_count, author)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"""

  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[EmailCampaignsTable] = []

#=================================================================================================================
class EmailContactsTable(Table):
  """Define the table 'email_contacts'
     Notes: subscribe_token - UUID which is used in urls for a user to manage their subscription.
            subscribe_status - Captures user's opt-out status. True - we can email, False - no email
  """
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(EmailContactsTable, self).__init__(name = "email_contacts", logger=logger,
                                        creationSql = """
                                            CREATE TABLE email_contacts (
                                                id serial NOT NULL PRIMARY KEY,
                                                email              TEXT NOT NULL,
                                                subscribe_token    TEXT NOT NULL,
                                                subscribe_status   BOOLEAN DEFAULT TRUE,
                                                CONSTRAINT email_contacts_email_unique UNIQUE (email),
                                                CONSTRAINT email_contacts_token_unique UNIQUE (subscribe_token)
                                                );
                                        """)
    self.insertSql = """INSERT INTO email_contacts (email, subscribe_token) VALUES (%s, %s) RETURNING id"""
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[EmailContactsTable] = []

#=================================================================================================================
class EmailCampaignsContactsTable(Table):
  """Define the table 'email_campaigns_contacts'
     Notes: Mapping table many to many
  """
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(EmailCampaignsContactsTable, self).__init__(name = "email_campaigns_contacts", logger=logger,
                                        creationSql = """
                                            CREATE TABLE email_campaigns_contacts (
                                                email_campaigns_id INTEGER REFERENCES email_campaigns (id),
                                                email_contacts_id  INTEGER REFERENCES email_contacts (id),
                                            CONSTRAINT email_campaigns_contacts_mapping_unique UNIQUE (email_campaigns_id, email_contacts_id)
                                            );
                                        """)
    self.insertSql = """INSERT INTO TABLENAME (email_campaigns_id, email_contacts) VALUES (%s, %s) RETURNING id"""
  
  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[EmailCampaignsContactsTable] = [EmailCampaignsTable, EmailContactsTable]

#=================================================================================================================
class SignatureProductdimsTable(Table):
  """Define the table 'signature_productdims'"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__ (self, logger, **kwargs):
    super(SignatureProductdimsTable, self).__init__(name = "signature_productdims", logger=logger,
                                        creationSql = """
                                            CREATE TABLE signature_productdims (
                                              signature text not null,
                                              productdims_id integer not null,
                                            UNIQUE (signature, productdims_id));
                                        """)
    self.insertSql = """INSERT INTO TABLENAME (signature, productdims_id) values (%s, %d)"""

  #-----------------------------------------------------------------------------------------------------------------
  def updateDefinition(self, databaseCursor):
    if socorro_pg.tablesMatchingPattern(self.name) == []:
      #this table doesn't exist yet, create it
      self.create(databaseCursor)

databaseDependenciesForSetup[SignatureProductdimsTable] = [TopCrashesBySignatureTable,ProductDimsTable]


#-----------------------------------------------------------------------------------------------------------------
def connectToDatabase(config, logger):
  databaseDSN = "host=%(databaseHost)s dbname=%(databaseName)s user=%(databaseUserName)s password=%(databasePassword)s" % config
  databaseConnection = pg.connect(databaseDSN)
  #databaseCursor = databaseConnection.cursor(cursor_factory=socorro_psy.LoggingCursor)
  #databaseCursor.setLogger(logger)
  databaseCursor = databaseConnection.cursor()
  return (databaseConnection, databaseCursor)

#-----------------------------------------------------------------------------------------------------------------
def setupDatabase(config, logger):
  databaseConnection, databaseCursor = connectToDatabase(config, logger)
  try:
    for aDatabaseObjectClass in getOrderedSetupList():
      aDatabaseObject = aDatabaseObjectClass(logger=logger)
      aDatabaseObject._createSelf(databaseCursor)
    databaseConnection.commit()
  except Exception,x:
    databaseConnection.rollback()
    socorro_util.reportExceptionAndAbort(logger)

#-----------------------------------------------------------------------------------------------------------------
def teardownDatabase(config,logger):
  global partitionCreationHistory
  databaseConnection,databaseCursor = connectToDatabase(config,logger)
  try:
    for databaseObjectClass in getOrderedSetupList():
      aDatabaseObject = databaseObjectClass(logger=logger)
      aDatabaseObject.drop(databaseCursor)
    databaseConnection.commit()
    partitionCreationHistory = set()
  except:
    databaseConnection.rollback()
    socorro_util.reportExceptionAndContinue(logger)

#-----------------------------------------------------------------------------------------------------------------
databaseObjectClassListForUpdate = [#ReportsTable,
                                    #DumpsTable,
                                    ExtensionsTable,
                                    FramesTable,
                                    ProcessorsTable,
                                    JobsTable,
                                    ]
#-----------------------------------------------------------------------------------------------------------------
def updateDatabase(config, logger):
  databaseConnection, databaseCursor = connectToDatabase(config, logger)
  try:
    #try:
      #databaseCursor.execute("CREATE LANGUAGE plpythonu")
    #except:
      #databaseConnection.rollback()
    for aDatabaseObjectClass in databaseObjectClassListForUpdate:
      aDatabaseObject = aDatabaseObjectClass(logger=logger)
      aDatabaseObject.updateDefinition(databaseCursor)
    databaseConnection.commit()
  except:
    databaseConnection.rollback()
    socorro_util.reportExceptionAndAbort(logger)

#-----------------------------------------------------------------------------------------------------------------
# list all the tables that should have weekly partitions pre-created. This is a subclass of all the PartitionedTables
# since it may be that some PartitionedTables should not be pre-created.
databaseObjectClassListForWeeklyPartitions = [ReportsTable,
                                              #DumpsTable,
                                              FramesTable,
                                              ExtensionsTable,
                                              PluginsReportsTable,
                                             ]
#-----------------------------------------------------------------------------------------------------------------
def createPartitions(config, logger):
  """
  Create a set of partitions for all the tables known to be efficient when they are created prior to being needed.
  see the list databaseObjectClassListForWeeklyParitions above
  """
  databaseConnection, databaseCursor = connectToDatabase(config, logger)
  try:
    for aDatabaseObjectClass in databaseObjectClassListForWeeklyPartitions:
      weekIterator = mondayPairsIteratorFactory(config.startDate, config.endDate)
      aDatabaseObject = aDatabaseObjectClass(logger=logger)
      aDatabaseObject.createPartitions(databaseCursor, weekIterator)
      databaseConnection.commit()
  except:
    databaseConnection.rollback()
    socorro_util.reportExceptionAndAbort(logger)

